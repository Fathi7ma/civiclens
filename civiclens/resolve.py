"""Location resolution: free text or coordinates -> a fully sourced place.

Resolving a location is the step that makes CivicLens verifiable. Every field
in the returned :class:`ResolvedPlace` carries the dataset it came from, so the
answer can cite it.

Socrata spatial strategy (Austin instance)
------------------------------------------
``within_circle`` and ``within_distance`` are **not available** on this portal,
so we use a coarse ``within_box`` server-side filter and then precise local
tests (point-in-polygon / haversine).
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import geo
from .config import CITY, dataset
from .socrata import SocrataError, get_client

log = logging.getLogger("civiclens.resolve")

_SQL_LIKE_ESCAPE = re.compile(r"([%'])")


def escape_like(value: str) -> str:
    return _SQL_LIKE_ESCAPE.sub(r"\\\1", value or "")


@dataclass
class Place:
    """A source-aware geographic record."""

    label: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    address: Optional[str] = None
    distance_m: Optional[float] = None
    #: False when the geocoder only matched a street segment rather than the
    #: exact address asked for. Answers must say so: 1201 and 1700 Nueces St
    #: can have entirely different zoning and flood zones.
    exact: bool = True
    #: What the user actually asked for, when it differs from ``address``.
    queried: Optional[str] = None
    #: dataset_key -> (record_id, detail dict, query url)
    evidence: Dict[str, Tuple[str, Dict[str, Any], str]] = field(default_factory=dict)

    def add(self, dataset_key: str, record_id: str, detail: Dict[str, Any], url: str) -> None:
        self.evidence[dataset_key] = (record_id, detail, url)


@dataclass
class ResolvedLocation:
    """Everything CivicLens knows about where the user is asking about."""

    input_text: str
    method: str  # "coordinates" | "address" | "corridor" | "district" | "city_center"
    place: Optional[Place] = None
    parcel: Optional[Place] = None
    zoning: Optional[Place] = None
    district: Optional[Place] = None
    neighborhood: Optional[Place] = None
    flood: Optional[Place] = None
    notes: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    @property
    def lat(self) -> Optional[float]:
        if self.place and self.place.lat is not None:
            return self.place.lat
        return CITY["center"]["lat"]  # type: ignore[index]

    @property
    def lon(self) -> Optional[float]:
        if self.place and self.place.lon is not None:
            return self.place.lon
        return CITY["center"]["lon"]  # type: ignore[index]

    @property
    def has_point(self) -> bool:
        return bool(self.place and self.place.lat is not None)

    @property
    def display(self) -> str:
        if self.place and self.place.address:
            return self.place.address
        if self.has_point:
            return "%.5f, %.5f" % (self.lat, self.lon)
        return "the City of Austin"


# ------------------------------------------------------------- geocoding ----
def _geocode_via_address_dataset(query: str) -> Optional[Place]:
    """Resolve free text against the city's geocoded address points.

    Three passes, most precise first: exact full-address match, house-number
    prefix match, then bare street name (used for corridor questions like
    "permits on Bouldin Ave", where any point on the street is a fair anchor).
    """
    client = get_client()
    normalized = geo.normalize_address(query)

    fields = "full_street_name,street_name,street_type,address,the_geom"

    def _place(row: Dict[str, Any], fallback: str) -> Optional[Place]:
        geom = geo.parse_geom(row.get("the_geom"))
        point = None
        for lon, lat in geo.geom_points(geom or {}):
            point = (lat, lon)
            break
        if point is None:
            return None
        addr = (row.get("full_street_name") or "").strip() or fallback
        place = Place(label=addr, lat=point[0], lon=point[1], address=addr, distance_m=0.0)
        url = client.build_url(dataset("addresses").id, {"$select": fields, "$limit": 1})
        place.add("addresses", addr, {"full_street_name": addr}, url)
        return place

    # 1. exact full address, trying each spelling variant the city uses
    for variant in geo.address_variants(normalized):
        rows = client.query(
            "addresses",
            select=fields,
            where="upper(full_street_name) = '%s'" % escape_like(variant),
            limit=5,
        )
        if rows:
            return _place(rows[0], normalized)

    tokens = normalized.split()
    if not tokens:
        return None

    # 2. house number + street prefix
    if tokens[0].isdigit() and len(tokens) > 1:
        house = tokens[0]
        stem = tokens[1]
        rows = client.query(
            "addresses",
            select=fields,
            where=(
                "upper(full_street_name) like '%s %s%%'" % (escape_like(house), escape_like(stem))
            ),
            limit=1,
        )
        if rows:
            return _place(rows[0], normalized)

    # 3. bare street name, e.g. "BOULDIN AVE" -> street_name "BOULDIN".
    #    This is a corridor anchor, NOT the address that was asked for, so it
    #    is flagged inexact and the answer must disclose it.
    stem = tokens[0] if len(tokens) == 1 else tokens[-2]
    if stem.isalpha() and len(stem) > 2:
        rows = client.query(
            "addresses",
            select=fields,
            where="upper(street_name) = '%s'" % escape_like(stem),
            limit=1,
        )
        if rows:
            place = _place(rows[0], normalized)
            if place is not None:
                place.exact = False
                place.queried = normalized.upper()
                place.label = place.queried
                return place

    return None


_NOMINATIM = "https://nominatim.openstreetmap.org/search"


def _geocode_via_nominatim(query: str) -> Optional[Place]:
    """Out-of-jurisdiction fallback (OpenStreetMap, no key)."""
    client = get_client()
    params = {"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us"}
    try:
        resp = client.session.get(_NOMINATIM, params=params, timeout=20)
        if resp.status_code != 200:
            return None
        hits = resp.json()
    except Exception as exc:  # pragma: no cover - network dependent
        log.debug("nominatim failed: %s", exc)
        return None
    if not hits:
        return None
    hit = hits[0]
    try:
        lat = float(hit["lat"])
        lon = float(hit["lon"])
    except (KeyError, ValueError):
        return None
    bbox = CITY["bbox"]  # type: ignore[index]
    if not (bbox["min_lat"] <= lat <= bbox["max_lat"] and bbox["min_lon"] <= lon <= bbox["max_lon"]):
        # Outside Austin. CivicLens only claims what its datasets cover.
        return Place(
            label=hit.get("display_name") or query,
            lat=lat,
            lon=lon,
            address=hit.get("display_name"),
        )
    return Place(label=hit.get("display_name") or query, lat=lat, lon=lon, address=hit.get("display_name"))


def geocode(query: str) -> Tuple[Optional[Place], List[str]]:
    """Resolve free text to a point. Returns (place, notes)."""
    notes: List[str] = []
    place = _geocode_via_address_dataset(query)
    if place:
        return place, notes
    notes.append("No exact match in the city's geocoded address dataset.")
    place = _geocode_via_nominatim(query)
    if place:
        notes.append("Resolved outside the city's address dataset; features may be incomplete.")
        return place, notes
    notes.append("Could not geocode that text.")
    return None, notes


# ------------------------------------------------------------- point parts --
def parcel_at(lat: float, lon: float) -> Tuple[Optional[Place], List[str]]:
    """Smallest parcel polygon containing the point."""
    notes: List[str] = []
    try:
        rows, url, strategy = rows_containing_point(
            "parcels",
            "the_geom",
            lon,
            lat,
            "prop_id,lu_desc,gen_lu_des,units,employees,shape_area,the_geom",
            radius_m=60,
        )
    except SocrataError as exc:
        return None, ["Parcel lookup failed: %s" % exc]

    if not rows:
        return None, ["No parcel polygon contains that point."]

    # If several parcels match (overlapping cadastre), take the smallest.
    def _area(row: Dict[str, Any]) -> float:
        try:
            return float(row.get("shape_area") or float("inf"))
        except (TypeError, ValueError):
            return float("inf")

    rows = sorted(rows, key=_area)
    row = rows[0]
    prop_id = str(row.get("prop_id") or row.get("objectid"))
    area = row.get("shape_area")
    if strategy == "within_box+local":
        notes.append("Parcel matched by local geometry filtering rather than the server index.")
    place = Place(label="Parcel %s" % prop_id, lat=lat, lon=lon, distance_m=0.0)
    detail = {
        "prop_id": prop_id,
        "lu_desc": row.get("lu_desc"),
        "gen_lu_des": row.get("gen_lu_des"),
        "units": row.get("units"),
        "employees": row.get("employees"),
        "shape_area": area,
        "acres": geo.sqft_to_acres(area),
        "match_strategy": strategy,
    }
    place.add("parcels", prop_id, detail, url)
    return place, notes


def nearest_address(lat: float, lon: float) -> Optional[Place]:
    """Closest geocoded address segment - the bridge from a point to a street."""
    client = get_client()
    bbox = geo.bbox_for_radius(lat, lon, 220)
    try:
        rows, url = client.box(
            "addresses",
            "the_geom",
            bbox,
            select="full_street_name,address,street_name,street_type,the_geom",
            limit=600,
        )
    except SocrataError:
        return None
    if not rows:
        return None
    best: Optional[Tuple[float, str, Dict[str, Any]]] = None
    for row in rows:
        geom = geo.parse_geom(row.get("the_geom"))
        for rlon, rlat in geo.geom_points(geom or {}):
            d = geo.haversine(lat, lon, rlat, rlon)
            if best is None or d < best[0]:
                best = (d, row.get("full_street_name") or "", row)
            break
    if best is None:
        return None
    dist, addr, row = best
    place = Place(label=addr, lat=lat, lon=lon, address=addr, distance_m=dist)
    place.add("addresses", addr, {"full_street_name": addr, "distance_m": dist}, url)
    return place


def zoning_for_address(address: str) -> Tuple[Optional[Place], List[str]]:
    """Official zoning designation for an address segment."""
    if not address:
        return None, ["No address to look up a zoning designation against."]
    client = get_client()
    normalized = geo.normalize_address(address)
    rows, url = client.query_with_url(
        "zoning",
        select="full_street_name,zoning_ztype,base_zone,base_zone_category,objectid",
        where="upper(full_street_name) = '%s'" % escape_like(normalized),
        limit=5,
    )
    if not rows:
        rows, url = client.query_with_url(
            "zoning",
            select="full_street_name,zoning_ztype,base_zone,base_zone_category,objectid",
            where="upper(full_street_name) like '%s%%'" % escape_like(normalized.split()[0] if normalized else ""),
            limit=5,
        )
    if not rows:
        return None, ["The zoning-by-address dataset has no segment matching that address."]
    row = rows[0]
    designation = row.get("zoning_ztype")
    place = Place(label="Zoning %s" % designation, address=row.get("full_street_name"))
    detail = {
        "full_street_name": row.get("full_street_name"),
        "zoning_ztype": designation,
        "base_zone": row.get("base_zone"),
        "base_zone_category": row.get("base_zone_category"),
    }
    place.add("zoning", str(row.get("objectid")), detail, url)
    return place, []


def district_at(lat: float, lon: float) -> Optional[Place]:
    try:
        rows, url, _strategy = rows_containing_point(
            "council_districts",
            "the_geom",
            lon,
            lat,
            "district_number,district_name,council_district_path,the_geom",
            radius_m=400,
            limit=20,
        )
    except SocrataError:
        return None
    if not rows:
        return None
    row = rows[0]
    place = Place(label=str(row.get("district_name")), lat=lat, lon=lon, distance_m=0.0)
    place.add(
        "council_districts",
        str(row.get("district_number")),
        {
            "district_number": row.get("district_number"),
            "district_name": row.get("district_name"),
            "council_district_path": row.get("council_district_path"),
        },
        url,
    )
    return place


def neighborhood_at(lat: float, lon: float) -> Optional[Place]:
    try:
        rows, url, _strategy = rows_containing_point(
            "neighborhoods",
            "the_geom",
            lon,
            lat,
            (
                "planning_area_name,neighborhood_planning_status,neighborhood_number,"
                "combined_npa,ordinance_number,the_geom"
            ),
            radius_m=1500,
            limit=20,
        )
    except SocrataError:
        return None
    if not rows:
        return None
    row = rows[0]
    place = Place(label=str(row.get("planning_area_name")), lat=lat, lon=lon, distance_m=0.0)
    place.add(
        "neighborhoods",
        str(row.get("planning_area_name")),
        {
            "planning_area_name": row.get("planning_area_name"),
            "neighborhood_planning_status": row.get("neighborhood_planning_status"),
            "neighborhood_number": row.get("neighborhood_number"),
            "ordinance_number": row.get("ordinance_number"),
        },
        url,
    )
    return place


def flood_at(lat: float, lon: float) -> Tuple[Optional[Place], List[str]]:
    """FEMA flood-hazard polygon containing the point, else the nearest one."""
    try:
        rows, url, strategy = rows_containing_point(
            "floodplain",
            "the_geom",
            lon,
            lat,
            "flood_zone,firm_panel,source_citation,effective_date,county,the_geom",
            radius_m=120,
            limit=200,
        )
    except SocrataError:
        return None, []

    notes: List[str] = []
    if rows:
        notes.append("Point falls inside a mapped FEMA flood-hazard polygon.")
        row = rows[0]
        inside = True
    else:
        inside = False
        row = None
        try:
            near_rows, near_url = get_client().box(
                "floodplain",
                "the_geom",
                geo.bbox_for_radius(lat, lon, 500),
                select="flood_zone,firm_panel,source_citation,effective_date,county,the_geom",
                limit=200,
            )
        except SocrataError:
            near_rows, near_url = [], url
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for candidate in near_rows:
            geom = geo.parse_geom(candidate.get("the_geom"))
            bb = geo.geom_bbox(geom or {})
            if not bb:
                continue
            mid_lat = (bb[0] + bb[2]) / 2.0
            mid_lon = (bb[1] + bb[3]) / 2.0
            d = geo.haversine(lat, lon, mid_lat, mid_lon)
            if best is None or d < best[0]:
                best = (d, candidate)
        if best is None:
            return None, ["No FEMA flood-hazard polygon within ~500 m of that point."]
        distance, row = best
        url = near_url
        notes.append(
            "Point is outside every mapped flood-hazard polygon; nearest mapped polygon is %s away."
            % geo.humanize_distance(distance)
        )

    place = Place(label=str(row.get("flood_zone")), lat=lat, lon=lon)
    detail = {
        "flood_zone": row.get("flood_zone"),
        "firm_panel": row.get("firm_panel"),
        "source_citation": row.get("source_citation"),
        "effective_date": row.get("effective_date"),
        "inside_polygon": inside,
        "match_strategy": strategy if inside else "nearest",
    }
    place.add("floodplain", str(row.get("firm_panel")), detail, url)
    return place, notes


def historic_at(address: Optional[str]) -> Optional[Place]:
    if not address:
        return None
    client = get_client()
    normalized = geo.normalize_address(address)
    try:
        rows, url = client.query_with_url(
            "historic",
            select="dpb,property,building_name,ordinance_link",
            where="upper(property) = '%s'" % escape_like(normalized),
            limit=5,
        )
    except SocrataError:
        return None
    if not rows:
        return None
    row = rows[0]
    place = Place(label=str(row.get("building_name") or row.get("property")), address=row.get("property"))
    place.add(
        "historic",
        str(row.get("dpb")),
        {
            "dpb": row.get("dpb"),
            "property": row.get("property"),
            "building_name": row.get("building_name"),
            "ordinance_link": row.get("ordinance_link"),
        },
        url,
    )
    return place


# ------------------------------------------------------------------ facade --
DISTRICT_RE = re.compile(r"\b(?:district|dist)\s*(\d{1,2})\b", re.I)


def resolve(
    text: str = "",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    street: Optional[str] = None,
) -> ResolvedLocation:
    """Resolve a question's location context into a fully sourced location."""
    loc = ResolvedLocation(input_text=text or "", method="city_center")

    if lat is not None and lon is not None:
        loc.method = "coordinates"
        loc.place = Place(label="%.5f, %.5f" % (lat, lon), lat=lat, lon=lon)
        loc.notes.append("Using the coordinates supplied with the question.")

    if loc.place is None and street:
        match = _geocode_via_address_dataset(street)
        if match:
            loc.place = match
            loc.method = "corridor"
            loc.notes.append(
                "Interpreting the question as being about the whole %s corridor; "
                "point-level answers use the address you tap on the map." % match.label
            )
        else:
            loc.notes.append("Could not locate that street in the city's address dataset.")

    if loc.place is None and text:
        # Pull the address phrase out of the sentence first. Handing the whole
        # question to the geocoder would make it fall through to the bare-street
        # pass and return some unrelated address on that street.
        candidate = _extract_address_phrase(text)
        if candidate:
            match = _geocode_via_address_dataset(candidate)
            if match:
                loc.place = match
                loc.method = "address"
                loc.notes.append("Read the location from your question as %r." % match.label)

    if loc.place is None and text and geo.address_like(text):
        match = _geocode_via_address_dataset(text)
        if match:
            loc.place = match
            loc.method = "address"

    if loc.place is None and lat is None:
        # "district 7" style questions still need an anchor; use the centroid
        # of the requested district when we can get one.
        dm = DISTRICT_RE.search(text or "")
        if dm:
            loc.notes.append("Anchored district %s questions to that district's boundary." % dm.group(1))

    # Enrich from the point onwards.
    plat, plon = loc.lat, loc.lon
    if loc.place is None:
        # Deliberately no gap entry here. The builders that actually need a
        # point already refuse with "give me an address", and duplicating that
        # as a data gap pollutes the Limits panel on citywide questions.
        return loc

    if not loc.place.address:
        near = nearest_address(plat, plon)
        if near and near.address:
            loc.place.address = near.address
            loc.place.evidence.setdefault("addresses", near.evidence.get("addresses", ("", {}, "")))

    target_address = loc.place.address

    # The five enrichment lookups are independent, so run them concurrently.
    # Each hits a different dataset, so this is the difference between a snappy
    # demo and a visibly slow one.
    tasks = {
        "parcel": lambda: parcel_at(plat, plon),
        "zoning": lambda: zoning_for_address(target_address) if target_address else (None, []),
        "district": lambda: (district_at(plat, plon), []),
        "neighborhood": lambda: (neighborhood_at(plat, plon), []),
        "flood": lambda: flood_at(plat, plon),
    }
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {name: pool.submit(fn) for name, fn in tasks.items()}
        results: Dict[str, Any] = {}
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except Exception as exc:  # one bad lookup must not sink the answer
                log.warning("enrichment %r failed: %s", name, exc)
                results[name] = (None, ["%s lookup failed: %s" % (name, exc)])

    loc.parcel, notes = results.get("parcel", (None, []))
    loc.notes.extend(notes or [])
    if loc.parcel is None:
        loc.missing.append("No parcel record matched that point.")

    loc.zoning, znotes = results.get("zoning", (None, []))
    loc.notes.extend(znotes or [])
    if loc.zoning is None:
        loc.missing.append("No zoning designation record matched that address segment.")

    loc.district, dnotes = results.get("district", (None, []))
    loc.notes.extend(dnotes or [])
    if loc.district is None:
        loc.missing.append("That point did not resolve inside a single-member council district.")

    loc.neighborhood, nnotes = results.get("neighborhood", (None, []))
    loc.notes.extend(nnotes or [])
    if loc.neighborhood is None:
        loc.missing.append("That point is not inside an adopted neighborhood planning area.")

    loc.flood, fnotes = results.get("flood", (None, []))
    loc.notes.extend(fnotes or [])

    # Disclose a corridor-level match loudly. A street segment several hundred
    # numbers away can have different zoning and a different flood zone, so
    # this must not be allowed to read as an exact answer.
    place = loc.place
    if place is not None and not place.exact:
        asked_number = geo.parse_house_number(place.queried or "")
        found_number = geo.parse_house_number(place.address or "")
        loc.missing.append(
            "%s is not in the city's geocoded address dataset. The records below come from the "
            "nearest segment it does hold, %s%s."
            % (
                place.queried,
                place.address,
                ". Treat these as a corridor indication, not a parcel-specific finding."
                if asked_number and found_number and asked_number != found_number
                else ".",
            )
        )

    return loc


_ADDRESS_PHRASE_RE = re.compile(
    r"\b(\d{1,6}\s+(?:[A-Za-z][A-Za-z.'-]*\s+){0,4}"
    r"(?:street|st|road|rd|avenue|ave|boulevard|blvd|drive|dr|lane|ln|court|ct|circle|cir|way|"
    r"terrace|ter|place|pl|parkway|pkwy|loop|trail|trl|path)\b\.?)",
    re.I,
)


def _extract_address_phrase(text: str) -> Optional[str]:
    m = _ADDRESS_PHRASE_RE.search(text or "")
    return m.group(1).strip() if m else None


def district_centroid(district_number: int) -> Optional[Dict[str, float]]:
    """Approximate centre of a council district, used to anchor aggregate Qs."""
    client = get_client()
    try:
        rows, _ = client.query_with_url(
            "council_districts",
            select="district_number,the_geom",
            where="district_number = %d" % district_number,
            limit=1,
        )
    except SocrataError:
        return None
    if not rows:
        return None
    geom = geo.parse_geom(rows[0].get("the_geom"))
    bb = geo.geom_bbox(geom or {})
    if not bb:
        return None
    return {"lat": (bb[0] + bb[2]) / 2.0, "lon": (bb[1] + bb[3]) / 2.0}

# ------------------------------------------------------ spatial resolution --
# ``intersects`` is the right primary predicate: evaluated server-side, exact,
# and fast. ``within_box`` is only a fallback for the cases where the
# intersection index misses - it returns a coarse superset that we then filter
# locally with a precise point-in-polygon test.
def rows_containing_point(
    dataset_key: str,
    geom_field: str,
    lon: float,
    lat: float,
    select: str,
    radius_m: float = 300.0,
    limit: int = 500,
) -> Tuple[List[Dict[str, Any]], str, str]:
    """Rows whose geometry contains the point.

    Returns ``(rows, query_url, strategy)`` where strategy is one of
    ``intersects``, ``within_box+local`` or ``none``.
    """
    client = get_client()

    try:
        rows, url = client.query_with_url(
            dataset_key,
            select=select,
            where="intersects(%s, '%s')" % (geom_field, geo.point_str(lon, lat)),
            limit=limit,
        )
        if rows:
            return rows, url, "intersects"
        intersects_url = url
    except SocrataError:
        intersects_url = ""

    bbox = geo.bbox_for_radius(lat, lon, radius_m)
    rows, box_url = client.box(dataset_key, geom_field, bbox, select=select, limit=limit)
    if not rows:
        return [], intersects_url or box_url, "none"

    keep = []
    for row in rows:
        geom = geo.parse_geom(row.get(geom_field))
        if geom and geo.point_in_geom(lon, lat, geom):
            keep.append(row)
    if not keep:
        return [], intersects_url or box_url, "none"
    return keep, box_url, "within_box+local"

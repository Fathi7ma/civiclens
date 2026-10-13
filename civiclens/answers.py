"""Answer builders - one function per intent.

Each builder follows the same contract: query only real datasets, attach a
reproducible citation for every claim, and say plainly when the data cannot
answer the question.
"""

from __future__ import annotations

import logging
import re
import statistics
from typing import Any, Dict, List, Optional, Tuple

from . import geo, kb, zoning
from .config import dataset
from .engine import (
    Answer,
    Fact,
    Slots,
    add_kb_refs,
    build_map,
    cite_place,
    cite_query,
    _finalize,
)
from .resolve import ResolvedLocation
from .socrata import SocrataError, get_client

log = logging.getLogger("civiclens.answers")


# --------------------------------------------------------------- utilities --
def _evidence(place: Optional[Any], key: str) -> Dict[str, Any]:
    if place is None:
        return {}
    entry = place.evidence.get(key)
    return entry[1] if entry else {}


def _fmt(value: Any) -> Any:
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return round(value, 4)
    if isinstance(value, str):
        return value.strip()
    return value


def _parcel_facts(loc: ResolvedLocation, answer: Answer) -> None:
    ev = _evidence(loc.parcel, "parcels")
    if not ev:
        return
    if ev.get("prop_id"):
        answer.facts.append(Fact("Parcel ID", str(ev["prop_id"]), dataset_key="parcels"))
    if ev.get("lu_desc"):
        answer.facts.append(Fact("Recorded land use", str(ev["lu_desc"]), dataset_key="parcels"))
    acres = ev.get("acres")
    if acres is not None:
        area = ev.get("shape_area")
        note = "%.0f sq ft" % float(area) if area is not None else None
        answer.facts.append(Fact("Site area", "%s acres" % acres, note=note, dataset_key="parcels"))
    if ev.get("units"):
        answer.facts.append(Fact("Units on parcel", _fmt(ev["units"]), dataset_key="parcels"))


def _zoning_facts(loc: ResolvedLocation, answer: Answer) -> Optional[zoning.ZoningReading]:
    ev = _evidence(loc.zoning, "zoning")
    if not ev:
        return None
    designation = ev.get("zoning_ztype")
    reading = zoning.read_designation(designation, ev.get("base_zone_category"))
    if designation:
        answer.facts.append(
            Fact("Zoning designation", str(designation), dataset_key="zoning")
        )
    if ev.get("base_zone_category"):
        note = reading.category_summary if reading else None
        answer.facts.append(
            Fact(
                "District category",
                str(ev["base_zone_category"]),
                note=note,
                dataset_key="zoning",
            )
        )
    if reading and reading.suffixes:
        answer.facts.append(
            Fact(
                "Overlays / suffixes",
                "-".join(reading.suffixes),
                note="; ".join(n["meaning"] for n in reading.overlay_notes) or None,
                dataset_key="zoning",
            )
        )
    return reading


def _context_facts(loc: ResolvedLocation, answer: Answer) -> None:
    d = _evidence(loc.district, "council_districts")
    if d.get("district_name"):
        answer.facts.append(Fact("Council district", str(d["district_name"]), dataset_key="council_districts"))
    n = _evidence(loc.neighborhood, "neighborhoods")
    if n.get("planning_area_name"):
        note = n.get("neighborhood_planning_status")
        answer.facts.append(
            Fact(
                "Neighborhood planning area",
                str(n["planning_area_name"]),
                note=str(note) if note else None,
                dataset_key="neighborhoods",
            )
        )
    f = _evidence(loc.flood, "floodplain")
    if f.get("flood_zone"):
        info = kb.flood_zone_info(str(f["flood_zone"]))
        answer.facts.append(
            Fact(
                "FEMA flood zone",
                info["label"] if info else str(f["flood_zone"]),
                note=(info["meaning"] if info else None),
                dataset_key="floodplain",
            )
        )


def _cite_core(loc: ResolvedLocation, answer: Answer) -> None:
    cite_place(loc.zoning, answer)
    cite_place(loc.parcel, answer)
    cite_place(loc.district, answer)
    cite_place(loc.neighborhood, answer)
    cite_place(loc.flood, answer)


# ------------------------------------------------------- property profile ---
def build_property_profile(
    question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float
) -> Answer:
    a = Answer(
        question=question,
        intent="property_profile",
        headline="Property record",
        summary="",
    )
    if not loc.has_point:
        a.refused = True
        a.headline = "I need an address or a map point"
        a.summary = (
            "CivicLens can only describe a specific parcel once it knows where you mean. "
            "Drop a pin on the map or give me a street address and I will pull the parcel, "
            "zoning, flood and permit records for it."
        )
        a.follow_ups = [
            "What is the zoning at 1507 Bouldin Ave?",
            "Is 1507 Bouldin Ave in a floodplain?",
        ]
        return _finalize(a, loc, 0.2, kb_conf)

    _parcel_facts(loc, a)
    reading = _zoning_facts(loc, a)
    _context_facts(loc, a)

    parts: List[str] = []
    zev = _evidence(loc.zoning, "zoning")
    if zev.get("zoning_ztype"):
        parts.append(str(zev["zoning_ztype"]))
    pev = _evidence(loc.parcel, "parcels")
    if pev.get("acres") is not None:
        parts.append("%s acres" % pev["acres"])
    dev = _evidence(loc.district, "council_districts")
    if dev.get("district_name"):
        parts.append(str(dev["district_name"]))

    a.headline = "%s%s" % (loc.display, (" - " + " - ".join(parts)) if parts else "")
    sentences: List[str] = []
    if pev.get("lu_desc"):
        sentences.append("The city's land database records this parcel as %s." % pev["lu_desc"])
    if zev.get("zoning_ztype"):
        if reading and reading.family_label:
            sentences.append(
                "Its zoning designation is %s, which puts it in the %s."
                % (zev["zoning_ztype"], reading.family_label.lower())
            )
        else:
            sentences.append("Its zoning designation is %s." % zev["zoning_ztype"])
    if pev.get("acres") is not None:
        sentences.append("The parcel covers %s acres." % pev["acres"])
    fz = _evidence(loc.flood, "floodplain")
    if fz.get("flood_zone"):
        info = kb.flood_zone_info(str(fz["flood_zone"]))
        if info:
            sentences.append(info["meaning"])
    if not sentences:
        sentences.append(
            "The city's parcel layer did not return a record for this exact point, so there is "
            "nothing I can honestly describe here yet."
        )
    a.summary = " ".join(sentences)
    a.follow_ups = [
        "What permits have been issued at %s?" % loc.display,
        "Is %s in a floodplain?" % loc.display,
        "Can I build a duplex at %s?" % loc.display,
    ]
    if not zev:
        a.data_gaps.append("No zoning designation could be matched to this address segment.")
    _cite_core(loc, a)
    add_kb_refs(question, a)
    return _finalize(a, loc, 0.9 if (pev and zev) else 0.55, kb_conf)


# -------------------------------------------------- zoning / what can I do --
_WANTED_USE_RULES: List[Tuple[str, List[str]]] = [
    ("duplex", ["duplex", "two unit", "2 unit", "two-unit", "two houses", "second house", "second home"]),
    ("adu", ["adu", "accessory dwelling", "granny flat", "backyard cottage", "cottage", "in-law", "in law", "secondary unit", "casita"]),
    ("apartment", ["apartment", "multifamily", "multi family", "multi-family", "fourplex", "triplex", "condo", "condominium"]),
    ("shed", ["shed", "garage", "carport", "outbuilding", "storage building"]),
    ("str", ["short term rental", "short-term rental", "str ", " airbnb", "vacation rental"]),
    ("commercial", ["shop", "store", "restaurant", "cafe", "office", "retail", "business", "bar"]),
    ("subdivide", ["subdivide", "split the lot", "split lot", "divide the lot", "tear down", "teardown"]),
    ("extension", ["extend", "extension", "add a room", "addition", "second story", "second-storey", "build up"]),
]


def wanted_use(question: str) -> Optional[str]:
    q = " " + (question or "").lower() + " "
    for label, terms in _WANTED_USE_RULES:
        if any(t in q for t in terms):
            return label
    return None


USE_HEADLINES = {
    "duplex": "a duplex",
    "adu": "an accessory dwelling unit",
    "apartment": "multifamily housing",
    "shed": "a detached outbuilding",
    "str": "a short-term rental",
    "commercial": "commercial use",
    "subdivide": "subdivision of the lot",
    "extension": "an extension or addition",
}


_ADU_NOTE = (
    "A second dwelling is possible as an *accessory* dwelling unit rather than as a second "
    "primary structure. ADUs are a separate approval with their own conditions, and the "
    "recent ordinance history in Austin has changed their rules several times, so treat the "
    "code link below as the thing to read."
)

_USE_GUIDANCE: Dict[str, Dict[str, Optional[str]]] = {
    "duplex": {
        "single family": "No. A duplex is a second primary structure, and single-family base districts allow one primary dwelling per lot.",
        "estate residential": "No. Estate residential is a single-dwelling district.",
        "multi family": "Likely yes, but the number of units you may build is capped by the district's density table, not by anything CivicLens can compute from the zoning designation alone.",
        "mixed use": "Likely yes in principle, with residential use in a mixed-use district usually still conditioned by the district table and any conditional overlay.",
        "commercial": "Depends on the district. Many commercial districts do not permit residential use at all, or do so conditionally.",
        "office": "Depends on the district.",
        "industrial": "No.",
        "planned unit development": "Cannot be answered from the zoning designation alone. A PUD is governed by its own recorded plan and adopting ordinance, which set the actual use rules for that tract.",
        "transit oriented development": "Depends on the district table for the specific subdistrict.",
    },
    "adu": {
        "single family": _ADU_NOTE,
        "estate residential": _ADU_NOTE,
        "multi family": "ADU rules still apply as a separate process on top of the district's residential rules.",
        "mixed use": _ADU_NOTE,
        "planned unit development": _ADU_NOTE,
    },
}


def build_zoning_feasibility(
    question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float
) -> Answer:
    a = Answer(
        question=question,
        intent="zoning_feasibility",
        headline="Zoning feasibility",
        summary="",
    )
    use = wanted_use(question)
    use_label = USE_HEADLINES.get(use or "", "that project")

    if not loc.has_point:
        a.refused = True
        a.headline = "I need an address before I can answer that"
        a.summary = (
            "Whether you can build depends entirely on which parcel you are asking about, and "
            "the answer changes street to street. Give me an address or drop a pin on the map "
            "and I will read the actual zoning designation and tell you what it does and does "
            "not establish."
        )
        a.follow_ups = ["Can I build a duplex at 1507 Bouldin Ave?"]
        return _finalize(a, loc, 0.2, kb_conf)

    _parcel_facts(loc, a)
    reading = _zoning_facts(loc, a)
    _context_facts(loc, a)
    _cite_core(loc, a)
    kb_conf = add_kb_refs(question, a) or kb_conf

    zev = _evidence(loc.zoning, "zoning")
    if not zev:
        a.refused = True
        a.headline = "I could not find a zoning record for %s" % loc.display
        a.summary = (
            "The city's zoning-by-address dataset has no segment matching that location, so I "
            "will not guess a designation. Try a nearby address with a house number, or call "
            "Development Services and ask for a zoning verification."
        )
        a.data_gaps.append("Zoning-by-address lookup returned no record.")
        a.follow_ups = ["Is 1507 Bouldin Ave in a floodplain?"]
        return _finalize(a, loc, 0.15, kb_conf)

    designation = str(zev.get("zoning_ztype"))
    family = reading.family if reading else None

    guidance = None
    if use and reading:
        table = _USE_GUIDANCE.get(use, {})
        guidance = table.get(family or "")

    a.headline = "Can you build %s at %s?" % (use_label, loc.display)
    lead = "The zoning designation at %s is **%s**" % (loc.display, designation)
    if reading and reading.family_label:
        lead += " (%s)" % reading.family_label
    lead += "."

    if guidance:
        body = guidance
    elif family == "planned unit development":
        body = (
            "This is a planned unit development, so the base zoning table is not what governs it. "
            "The recorded plan for the district controls, and that document is not in the open-data "
            "portal - you would need to request it from Development Services."
        )
    elif reading and reading.category_summary:
        body = "The district category is %s - %s" % (
            zev.get("base_zone_category"),
            reading.category_summary.lower(),
        )
    else:
        body = (
            "I can tell you the designation, but this is not a family CivicLens has a confident "
            "reading for, so I will not characterise it."
        )

    a.summary = "%s %s" % (lead, body)

    # Evidence beats theory: show what has actually been permitted here.
    adu_permits: List[Dict[str, Any]] = []
    recent = _permits_near(loc.lat, loc.lon, 120, limit=60)
    if recent:
        cite_query(
            "permits",
            {
                "$select": "permit_number,permit_type_desc,description,issue_date,original_address1",
                "$where": "latitude > %s and latitude < %s and longitude > %s and longitude < %s"
                % (
                    geo.bbox_for_radius(loc.lat, loc.lon, 120)[0],
                    geo.bbox_for_radius(loc.lat, loc.lon, 120)[2],
                    geo.bbox_for_radius(loc.lat, loc.lon, 120)[1],
                    geo.bbox_for_radius(loc.lat, loc.lon, 120)[3],
                ),
                "$limit": 60,
            },
            a,
            title="Issued permits within ~120 m",
            count=len(recent),
        )
        adu_permits = [
            p for p in recent if re.search(r"adu|duplex|secondary|second dwelling", str(p.get("description")), re.I)
        ]
        if recent:
            a.facts.append(Fact("Permits within ~120 m", len(recent), dataset_key="permits"))
        if adu_permits:
            lines = [
                "- %s (%s), issued %s"
                % (p.get("permit_number"), str(p.get("description"))[:90], str(p.get("issue_date"))[:10])
                for p in adu_permits[:4]
            ]
            a.summary += "\n\n**What has actually been approved nearby:**\n%s" % "\n".join(lines)

    route = kb.CODE_ROUTES["zoning_dimensional"]
    a.caveats.append(
        "CivicLens reports the designation and the governing code; it deliberately does not "
        "recite setback, height or floor-area-ratio numbers from memory, because those are set "
        "by the district table and overlays in %s." % route["where"]
    )
    if reading and reading.unknown_suffixes:
        a.caveats.append(
            "Suffix(es) %s are not in CivicLens's glossary; confirm their effect in the code."
            % ", ".join(reading.unknown_suffixes)
        )
    a.kb_refs.append(
        {
            "id": "route:zoning_dimensional",
            "title": route["where"],
            "topic": route["topic"],
            "source_label": "Development Code",
            "source_url": route["url"],
            "score": 1.0,
        }
    )
    a.follow_ups = [
        "What permits have been issued at %s?" % loc.display,
        "Is %s in a floodplain?" % loc.display,
        "What is the council district for %s?" % loc.display,
    ]
    return _finalize(a, loc, 0.82, kb_conf)


# ------------------------------------------------------------------- flood --
def build_flood(question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float) -> Answer:
    a = Answer(question=question, intent="flood", headline="Flood risk", summary="")
    if not loc.has_point:
        a.refused = True
        a.headline = "I need a location to check flood risk"
        a.summary = (
            "FEMA flood zones are polygon data, so the answer is specific to a point. Give me an "
            "address or tap the map and I will look up the effective FEMA panel."
        )
        return _finalize(a, loc, 0.2, kb_conf)

    if loc.flood is None:
        a.refused = True
        a.headline = "No mapped flood data at %s" % loc.display
        a.summary = (
            "The Greater Austin FEMA floodplain dataset returned no polygon near that point. "
            "That is not the same as 'not in a floodplain' - unstudied areas exist, especially "
            "downstream of dams. Check FEMA's map service centre for the effective panel."
        )
        a.caveats.append(
            "Absence from a FEMA panel is not a flood-safety determination."
        )
        a.kb_refs.append(
            {
                "id": "route:floodplain",
                "title": kb.CODE_ROUTES["floodplain"]["where"],
                "topic": "flood",
                "source_label": "FEMA",
                "source_url": kb.CODE_ROUTES["floodplain"]["url"],
                "score": 1.0,
            }
        )
        _cite_core(loc, a)
        return _finalize(a, loc, 0.25, kb_conf)

    ev = _evidence(loc.flood, "floodplain")
    code = str(ev.get("flood_zone"))
    info = kb.flood_zone_info(code)
    cite_place(loc.flood, a)
    add_kb_refs(question, a)

    if info:
        a.headline = "%s - %s" % (loc.display, info["label"])
        a.summary = "%s %s" % (info["label"], info["meaning"])
        a.facts.append(Fact("FEMA flood zone", info["label"], dataset_key="floodplain"))
        a.facts.append(Fact("Risk class", info["risk"].title(), dataset_key="floodplain"))
        if info.get("note"):
            a.caveats.append(str(info["note"]))
        if ev.get("firm_panel"):
            a.facts.append(Fact("FIRM panel", str(ev["firm_panel"]), dataset_key="floodplain"))
        if ev.get("source_citation"):
            a.facts.append(
                Fact("FEMA study", str(ev["source_citation"]), dataset_key="floodplain")
            )
        if ev.get("inside_polygon") is False:
            a.caveats.append(
                "The point sits outside the mapped polygons; the zone shown is the nearest "
                "mapped polygon, not a determination for this parcel."
            )
    else:
        a.headline = "%s - unrecognised flood zone code %r" % (loc.display, code)
        a.summary = (
            "The dataset returned the zone code %r, which is not in CivicLens's FEMA glossary. "
            "CivicLens will not interpret a code it cannot verify - read it against the FEMA "
            "legend on the cited panel." % code
        )
        a.refused = True

    if info and info["risk"] in ("high", "extreme"):
        a.follow_ups.append("What permits have been issued at %s?" % loc.display)
    a.follow_ups.append("What is the council district for %s?" % loc.display)
    _cite_core(loc, a)
    return _finalize(a, loc, 0.88, kb_conf)


# ----------------------------------------------------------------- permits --
PERMIT_LIMIT = 200

PERMIT_SELECT = (
    "permit_number,permit_type_desc,description,issue_date,original_address1,"
    "council_district,contractor_company_name,permit_class,total_job_valuation,"
    "total_new_add_sqft,housing_units,latitude,longitude"
)

_STREET_RE_PERMITS = re.compile(
    r"^\s*(\d{1,6})\s+(?:[A-Z0-9.'-]+\s+){0,4}"
    r"(ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|BLVD|BOULEVARD|LN|LANE|CT|COURT|WAY|PL|PLACE|LOOP|PKWY|TRL|TRAIL)\b"
)


def _street_phrase(address: str) -> Optional[str]:
    """'1507 BOULDIN AVE' -> 'BOULDIN AVE' (the part shared by the whole street)."""
    if not address:
        return None
    m = _STREET_RE_PERMITS.match(address.strip().upper())
    if not m:
        tokens = address.strip().upper().split()
        return " ".join(tokens[-2:]) if len(tokens) >= 2 else None
    return m.group(0).strip().upper().split(" ", 1)[1] if " " in m.group(0) else m.group(0)


def _permits_on_street(street: str, address: str) -> Tuple[List[Dict[str, Any]], str]:
    """Permits whose site address sits on ``street``.

    Returns ``(rows, where_clause)``. The street name is the safer key: a
    permit's ``original_address1`` sometimes carries a unit or a building
    suffix that would defeat an exact address match.
    """
    client = get_client()
    street_norm = street.replace("'", "''")
    where = "upper(original_address1) like '%%%s%%'" % street_norm
    try:
        rows = client.query(
            "permits",
            select=PERMIT_SELECT,
            where=where,
            order="issue_date DESC",
            limit=PERMIT_LIMIT,
        )
    except SocrataError:
        return [], where
    return rows, where


def _count_matching(dataset_key: str, where: str) -> Optional[int]:
    """True size of a result set, so truncation can be disclosed honestly."""
    try:
        return get_client().count(dataset_key, where)
    except SocrataError:
        return None


def _sum_field(rows: List[Dict[str, Any]], field: str) -> float:
    total = 0.0
    for r in rows:
        try:
            total += float(r.get(field) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _permits_near(lat: float, lon: float, radius_m: float, limit: int = 100) -> List[Dict[str, Any]]:
    client = get_client()
    bbox = geo.bbox_for_radius(lat, lon, radius_m)
    try:
        rows = client.query(
            "permits",
            select=PERMIT_SELECT,
            where=geo.lonlat_bbox_to_where(bbox, "latitude", "longitude"),
            order="issue_date DESC",
            limit=min(limit, 400),
        )
    except SocrataError:
        return []
    return geo.within_radius(rows, lat, lon, radius_m, "latitude", "longitude")


def build_permits(question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float) -> Answer:
    a = Answer(question=question, intent="permits", headline="Permit history", summary="")
    if not loc.has_point:
        a.refused = True
        a.headline = "I need an address to pull permit history"
        a.summary = (
            "Permits are recorded against a project and a site address. Give me an address and I "
            "will list what the city has actually approved there."
        )
        return _finalize(a, loc, 0.2, kb_conf)

    radius = slots.radius_m or 150.0
    bbox = geo.bbox_for_radius(loc.lat, loc.lon, radius)

    # Two complementary strategies, because the permit dataset is imperfect:
    # many rows have null latitude/longitude, so a radius filter alone silently
    # drops most of the record. Matching the site address text is exact.
    address = (loc.place.address if loc.place else "") or ""
    street = _street_phrase(address)
    rows: List[Dict[str, Any]] = []

    if street:
        rows, where = _permits_on_street(street, address)
        if rows:
            total_matching = _count_matching("permits", where)
            cite_query(
                "permits",
                {
                    "$select": PERMIT_SELECT,
                    "$where": where,
                    "$order": "issue_date DESC",
                    "$limit": PERMIT_LIMIT,
                },
                a,
                title="Issued permits on %s" % street,
                count=total_matching,
            )
            if total_matching and total_matching > len(rows):
                truncated = total_matching - len(rows)
                a.caveats.append(
                    "Showing the %d most recent of %s permits on this street; %d older records were "
                    "not included." % (len(rows), "{:,}".format(total_matching), truncated)
                )
                rows_total = total_matching
            else:
                rows_total = len(rows)
        else:
            rows_total = 0

    if not rows:
        rows = _permits_near(loc.lat, loc.lon, radius, limit=300)
        rows_total = len(rows)
        cite_query(
            "permits",
            {
                "$select": PERMIT_SELECT,
                "$where": geo.lonlat_bbox_to_where(bbox, "latitude", "longitude"),
                "$order": "issue_date DESC",
                "$limit": 300,
            },
            a,
            title="Issued permits within %s" % geo.humanize_distance(radius),
            count=len(rows),
        )

    add_kb_refs(question, a)
    _cite_core(loc, a)

    scope = "on %s" % street if street else "within %s" % geo.humanize_distance(radius)

    if not rows:
        a.refused = True
        a.headline = "No issued permits found %s" % scope
        a.summary = (
            "The issued-permit dataset returned nothing for that location. Two honest caveats: "
            "many permit records carry no coordinates, so a radius search misses them, and older "
            "records predate this dataset. An empty result is not proof that nothing was built."
        )
        a.follow_ups = ["What is the zoning at %s?" % loc.display]
        return _finalize(a, loc, 0.35, kb_conf)

    years: Dict[str, int] = {}
    kinds: Dict[str, int] = {}
    for r in rows:
        yrs = str(r.get("issue_date") or "")[:4]
        if yrs and yrs != "None":
            years[yrs] = years.get(yrs, 0) + 1
        kind = str(r.get("permit_type_desc") or "Unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
    recent_year = max(years) if years else None
    newest = rows[0] if rows else {}
    newest_date = str(newest.get("issue_date") or "")[:10]

    a.headline = "%s permits %s" % ("{:,}".format(rows_total), scope)
    a.summary = (
        "The city has issued %s permits %s. The most recent shown, %s (%s), was issued %s. "
        "A permit record is evidence of what the city approved - it is not proof of what the code "
        "allows today, and a missing permit does not prove that nothing was built."
        % (
            "{:,}".format(rows_total),
            scope,
            newest.get("permit_number"),
            str(newest.get("description") or newest.get("permit_type_desc"))[:90],
            newest_date,
        )
    )
    a.facts.append(Fact("Permits on record", "{:,}".format(rows_total), dataset_key="permits"))
    if recent_year:
        a.facts.append(
            Fact("Most recent year on record", "%s (%d permits)" % (recent_year, years[recent_year]),
                 dataset_key="permits")
        )
    top_kind = max(kinds.items(), key=lambda kv: kv[1]) if kinds else None
    if top_kind:
        a.facts.append(Fact("Most common permit type", "%s (%d)" % top_kind, dataset_key="permits"))

    # Project economics are the most interesting part of a permit record.
    valuation = _sum_field(rows, "total_job_valuation")
    if valuation:
        a.facts.append(
        Fact("Declared job value (shown records)", "$%s" % "{:,}".format(int(valuation)),
             note="Summed over the %d records displayed." % len(rows), dataset_key="permits")
    )
    added = _sum_field(rows, "total_new_add_sqft")
    if added:
        a.facts.append(Fact("New / added floor area", "%s sq ft" % "{:,}".format(int(added)), dataset_key="permits"))
    units = _sum_field(rows, "housing_units")
    if units:
        a.facts.append(Fact("Housing units authorised", int(units), dataset_key="permits"))

    notable = [r for r in rows if re.search(r"new construction|adu|duplex|remodel", str(r.get("description")), re.I)]
    if notable:
        a.summary += "\n\n**Notable scopes of work:**\n%s" % "\n".join(
            "- %s - %s (%s)"
            % (
                r.get("permit_number"),
                str(r.get("description"))[:100],
                str(r.get("issue_date") or "")[:10],
            )
            for r in notable[:5]
        )

    markers = [
        {
            "type": "marker",
            "lat": float(r["latitude"]),
            "lon": float(r["longitude"]),
            "label": "%s - %s" % (r.get("permit_number"), str(r.get("description"))[:70]),
            "kind": "permit",
        }
        for r in rows[:60]
        if r.get("latitude") and r.get("longitude")
    ]
    a.map = build_map(loc, markers=markers, zoom=16)
    a.follow_ups = ["What is the zoning at %s?" % loc.display, "Is %s in a floodplain?" % loc.display]
    return _finalize(a, loc, 0.85, kb_conf)


# --------------------------------------------------------- service requests --
_311_TYPE_MAP = {
    "pothole": ["pothole"],
    "graffiti": ["graffiti"],
    "trash": ["trash", "garbage", "collection", "missed"],
    "streetlight": ["streetlight", "street light"],
    "traffic signal": ["traffic signal", "signal"],
    "tree": ["tree"],
    "water main": ["water main"],
    "sidewalk": ["sidewalk"],
    "weeds": ["weed", "vegetation"],
    "signage": ["sign"],
    "bulky waste": ["bulky"],
}


def _service_where(service_type: Optional[str]) -> Tuple[Optional[str], str]:
    if not service_type:
        return None, "All service request types"
    keys = _311_TYPE_MAP.get(service_type.lower(), [service_type.lower()])
    clause = " or ".join("upper(sr_type_desc) like '%%%s%%'" % k.upper() for k in keys)
    return clause, "Service type matching %r" % service_type


def build_service_requests(
    question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float
) -> Answer:
    a = Answer(question=question, intent="service_requests", headline="311 service requests", summary="")
    add_kb_refs(question, a)

    if not loc.has_point:
        a.refused = True
        a.headline = "I need an address or street to look up 311 cases"
        a.summary = (
            "311 cases are recorded against a specific location. Give me an address or a street "
            "name and I will pull the cases filed there, including how long they took to close."
        )
        return _finalize(a, loc, 0.2, kb_conf)

    client = get_client()
    type_clause, type_desc = _service_where(slots.service_type)
    radius = slots.radius_m or 300.0
    bbox = geo.bbox_for_radius(loc.lat, loc.lon, radius)

    # The 311 dataset stores coordinates as sr_location_lat / sr_location_long,
    # not latitude/longitude like the permits dataset.
    parts = [
        "sr_number", "sr_type_desc", "sr_department_desc", "sr_location",
        "sr_status_desc", "sr_created_date", "sr_closed_date",
        "sr_location_street_name", "sr_location_street_number",
        "sr_location_council_district", "sr_location_lat", "sr_location_long",
    ]
    where = [geo.lonlat_bbox_to_where(bbox, "sr_location_lat", "sr_location_long")]
    if type_clause:
        where.append("(%s)" % type_clause)
    if slots.year:
        where.append("sr_created_date >= '%d-01-01T00:00:00' and sr_created_date < '%d-01-01T00:00:00'"
                     % (slots.year, slots.year + 1))
    where_sql = " and ".join(where)

    try:
        rows = client.query(
            "service_requests",
            select=",".join(parts),
            where=where_sql,
            order="sr_created_date DESC",
            limit=400,
        )
    except SocrataError as exc:
        a.refused = True
        a.headline = "The 311 dataset could not be queried"
        a.summary = "The open-data API rejected the query: %s" % exc
        return _finalize(a, loc, 0.1, kb_conf)

    cite_query(
        "service_requests",
        {
            "$select": ",".join(parts),
            "$where": where_sql,
            "$order": "sr_created_date DESC",
            "$limit": 400,
        },
        a,
        title="311 cases - %s" % type_desc,
        count=len(rows),
    )
    _cite_core(loc, a)

    scoped = geo.within_radius(rows, loc.lat, loc.lon, radius, "sr_location_lat", "sr_location_long")
    if not scoped:
        a.refused = True
        a.headline = "No 311 cases within %s of %s" % (geo.humanize_distance(radius), loc.display)
        a.summary = (
            "The 311 dataset returned no cases in that radius%s. Residents may simply not have "
            "reported anything there - a quiet record is not a positive signal."
            % (" for %s" % type_desc.lower() if slots.service_type else "")
        )
        a.follow_ups = ["What 311 activity is highest in the city right now?"]
        return _finalize(a, loc, 0.45, kb_conf)

    open_cases = [r for r in scoped if str(r.get("sr_status_desc") or "").lower() == "open"]
    closed = [r for r in scoped if r.get("sr_closed_date")]
    durations: List[float] = []
    for r in closed:
        try:
            c = _parse_ts(r.get("sr_created_date"))
            cl = _parse_ts(r.get("sr_closed_date"))
            if c and cl and cl >= c:
                durations.append((cl - c).total_seconds() / 86400.0)
        except (TypeError, ValueError):
            continue

    kinds: Dict[str, int] = {}
    for r in scoped:
        kinds[str(r.get("sr_type_desc"))] = kinds.get(str(r.get("sr_type_desc")), 0) + 1
    top = max(kinds.items(), key=lambda kv: kv[1]) if kinds else (None, 0)

    a.headline = "%d 311 cases within %s of %s" % (len(scoped), geo.humanize_distance(radius), loc.display)
    summary_bits = [
        "The city has %d 311 service requests on record within %s of %s%s."
        % (len(scoped), geo.humanize_distance(radius), loc.display,
           " matching %r" % slots.service_type if slots.service_type else "")
    ]
    if durations:
        med = statistics.median(durations)
        summary_bits.append(
            "Of the %d cases in this sample that have been closed, the median time to close was "
            "%.0f days." % (len(durations), med)
        )
    else:
        summary_bits.append("None of the cases in this sample have a recorded closure date.")
    if top[0]:
        summary_bits.append("The most common request type here is %s (%d cases)." % (top[0], top[1]))
    if open_cases:
        summary_bits.append("%d of these are still open." % len(open_cases))
    a.summary = " ".join(summary_bits)

    a.facts.append(Fact("Cases in range", len(scoped), dataset_key="service_requests"))
    a.facts.append(Fact("Still open", len(open_cases), dataset_key="service_requests"))
    if durations:
        a.facts.append(
            Fact(
                "Median days to close",
                "%.0f (n=%d closed cases)" % (statistics.median(durations), len(durations)),
                dataset_key="service_requests",
            )
        )
    if top[0]:
        a.facts.append(Fact("Most common type", "%s (%d)" % (top[0], top[1]), dataset_key="service_requests"))

    markers = [
        {
            "type": "marker",
            "lat": float(r["sr_location_lat"]),
            "lon": float(r["sr_location_long"]),
            "label": "%s - %s (%s)" % (r.get("sr_number"), r.get("sr_type_desc"), r.get("sr_status_desc")),
            "kind": "open" if str(r.get("sr_status_desc")).lower() == "open" else "closed",
        }
        for r in scoped[:60]
        if r.get("sr_location_lat") and r.get("sr_location_long")
    ]
    a.map = build_map(loc, markers=markers, zoom=15)
    a.caveats.append(
        "Resolution times are computed from the created and closed timestamps on this sample "
        "only; cases without a closure date are excluded rather than assumed instantaneous."
    )
    a.follow_ups = [
        "Which districts get the most 311 requests?",
        "What permits have been issued at %s?" % loc.display,
    ]
    return _finalize(a, loc, 0.8, kb_conf)


def _parse_ts(value: Any):
    from datetime import datetime

    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 6].rstrip("Z"), fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- analytics --
# Which dataset an aggregate should run over. Order matters: the first group
# whose trigger appears in the question wins.
_ANALYTICS_TARGETS: List[Tuple[Tuple[str, ...], str, str]] = [
    (("pothole", "311", "service request", "graffiti", "complaint", "code violation", "bulky"),
     "service_requests", "sr_type_desc"),
    (("permit", "build", "construction", "remodel"),
     "permits", "permit_type_desc"),
    (("zoning", "zoned", "district category"),
     "zoning", "base_zone_category"),
    (("flood", "floodplain"),
     "floodplain", "flood_zone"),
]

#: Group-by column per dataset when the question asks for a per-district split.
_DISTRICT_COLUMN = {
    "service_requests": "sr_location_council_district",
    "permits": "council_district",
}


def _analytics_target(question: str) -> Tuple[str, str, str]:
    """Pick (dataset, default group column, human trigger) for the question."""
    q = (question or "").lower()
    for triggers, ds, group in _ANALYTICS_TARGETS:
        for trigger in triggers:
            if trigger in q:
                return ds, group, trigger
    return "service_requests", "sr_type_desc", "311"


def build_analytics(question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float) -> Answer:
    a = Answer(question=question, intent="analytics", headline="Citywide pattern", summary="")
    q = (question or "").lower()
    client = get_client()
    add_kb_refs(question, a)

    ds_key, group_field, trigger = _analytics_target(question)
    by_district = bool(re.search(r"\bdistricts?\b", q))

    # "How many X were there in 2025?" wants a single total, not a ranking.
    # Detect that before building a group-by the question never asked for.
    wants_total = (
        bool(re.search(r"\bhow many\b|\btotal\b|\bcount\b|\bhow much\b", q))
        and not re.search(r"\bby\b|\bbreakdown\b|\bper\b|\bdistricts?\b|\bwhich\b|\bmost\b|\btop\b|\bcategor", q)
    )
    if by_district and ds_key in _DISTRICT_COLUMN:
        group_field = _DISTRICT_COLUMN[ds_key]

    where_parts: List[str] = []

    # Narrow to a service type when the question names one, e.g. "potholes".
    type_clause, type_desc = _service_where(slots.service_type)
    if type_clause and ds_key == "service_requests":
        where_parts.append("(%s)" % type_clause)

    if slots.year:
        if ds_key == "permits":
            where_parts.append("calendar_year_issued = %d" % slots.year)
        elif ds_key == "service_requests":
            where_parts.append(
                "sr_created_date >= '%d-01-01T00:00:00' and sr_created_date < '%d-01-01T00:00:00'"
                % (slots.year, slots.year + 1)
            )
    where = " and ".join(where_parts) if where_parts else None

    if wants_total:
        total = _count_matching(ds_key, where or "")
        cite_query(
            ds_key,
            {"$select": "count(1) as n", "$where": where} if where else {"$select": "count(1) as n"},
            a,
            title="Total records in %s" % dataset(ds_key).name,
            count=total,
        )
        label = "citywide"
        if slots.year:
            label = "in %d" % slots.year
        if type_clause and ds_key == "service_requests":
            label += ", %s" % type_desc.lower()
        a.headline = "%s %s record%s" % (
            "{:,}".format(total or 0), trigger, "" if (total or 0) == 1 else "s"
        )
        a.summary = (
            "There are %s matching records %s in %s. That is a raw tally over the whole dataset - "
            "open the citation to run the same count."
            % ("{:,}".format(total or 0), type_desc.lower() if (type_clause and ds_key == "service_requests") else "", label)
        )
        a.facts.append(Fact("Records", "{:,}".format(total or 0), dataset_key=ds_key))
        if slots.year:
            a.facts.append(Fact("Year", slots.year, dataset_key=ds_key))
        a.caveats.append(
            "A raw count says nothing about rate. Two districts can have identical counts and "
            "completely different problems if their populations differ."
        )
        a.follow_ups = ["Which council districts get the most %s?" % trigger]
        return _finalize(a, loc, 0.8, kb_conf)

    select = "%s,count(1) as n" % group_field
    params: Dict[str, Any] = {
        "$select": select,
        "$group": group_field,
        "$order": "n DESC",
        "$limit": 12,
    }
    if where:
        params["$where"] = where

    try:
        rows = client.aggregate(
            ds_key, select=select, group=group_field, order="n DESC", where=where, limit=12
        )
    except SocrataError as exc:
        a.refused = True
        a.headline = "That aggregate could not be run"
        a.summary = "The open-data API rejected the query: %s" % exc
        return _finalize(a, loc, 0.1, kb_conf)

    scope_desc = "citywide, %s" % type_desc.lower()
    if slots.year:
        scope_desc += ", %d" % slots.year
    if by_district:
        scope_desc += ", by council district"

    cite_query(
        ds_key,
        params,
        a,
        title="%s - %s" % (dataset(ds_key).name, scope_desc),
        count=len(rows),
    )

    if not rows:
        a.refused = True
        a.headline = "No rows for that aggregate"
        a.summary = "The query ran but returned nothing, so there is no pattern to report."
        return _finalize(a, loc, 0.3, kb_conf)

    def _n(row: Dict[str, Any]) -> int:
        try:
            return int(row.get("n") or 0)
        except (TypeError, ValueError):
            return 0

    rows = sorted(rows, key=_n, reverse=True)
    top_name = rows[0].get(group_field)
    top_n = _n(rows[0])
    total = sum(_n(r) for r in rows)
    if group_field in _DISTRICT_COLUMN.values():
        top_name = "District %s" % top_name if top_name not in (None, "") else "an unassigned district"

    a.headline = "%s leads %s (%s)" % (top_name, scope_desc, "{:,}".format(top_n))
    a.summary = (
        "Grouping the %s dataset by %s %s: %s is the largest group at %s records. The groups "
        "shown cover %s records in total. Every number here comes from one aggregate query - open "
        "the citation to reproduce the ranking yourself."
        % (
            dataset(ds_key).name,
            group_field.replace("sr_location_", "").replace("_", " "),
            scope_desc,
            top_name,
            "{:,}".format(top_n),
            "{:,}".format(total),
        )
    )
    for i, row in enumerate(rows[:8], start=1):
        label = row.get(group_field)
        if label in (None, ""):
            label = "unspecified"
        elif group_field in _DISTRICT_COLUMN.values():
            label = "District %s" % label
        a.facts.append(
            Fact("%d. %s" % (i, label), "{:,}".format(_n(row)), dataset_key=ds_key)
        )
    a.caveats.append(
        "Counts are raw record tallies. They do not adjust for how long a category has been "
        "tracked or for how many people live in each district, so a denser district will look "
        "worse without being worse."
    )
    a.follow_ups = [
        "Which council districts get the most 311 requests?",
        "What permits have been issued at %s?" % loc.display if loc.has_point else
        "What are the most common 311 request types in Austin?",
    ]
    return _finalize(a, loc, 0.75, kb_conf)


# ----------------------------------------------------------------- council --
def build_council(question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float) -> Answer:
    a = Answer(question=question, intent="council", headline="Council district", summary="")
    add_kb_refs(question, a)

    if loc.district is not None:
        ev = _evidence(loc.district, "council_districts")
        name = str(ev.get("district_name"))
        path = ev.get("council_district_path")
        a.headline = "%s is in %s" % (loc.display, name)
        a.summary = (
            "%s falls inside %s. Council members are elected from single-member districts, so "
            "this is the district that decides your representative." % (loc.display, name)
        )
        a.facts.append(Fact("Council district", name, dataset_key="council_districts"))
        if path:
            a.facts.append(Fact("Official district page", str(path), dataset_key="council_districts"))

        client = get_client()
        dist_num = ev.get("district_number")
        if dist_num is not None:
            try:
                permits_n = client.count("permits", "council_district = '%s'" % dist_num)
                cite_query(
                    "permits",
                    {"$select": "count(1) as n", "$where": "council_district = '%s'" % dist_num},
                    a,
                    title="Permits in %s" % name,
                    count=permits_n,
                )
                a.facts.append(Fact("Permits on record in district", "{:,}".format(permits_n), dataset_key="permits"))
            except SocrataError:
                pass
        cite_place(loc.district, a)
        a.caveats.append("District boundaries are redrawn by ordinance; verify against the current map.")
        _cite_core(loc, a)
        return _finalize(a, loc, 0.85, kb_conf)

    if slots.district is not None:
        a.refused = True
        a.headline = "No point supplied for district %d" % slots.district
        a.summary = (
            "You asked about district %d but did not give an address, so I cannot confirm which "
            "districts touch yours. Drop a pin and I will tell you." % slots.district
        )
        return _finalize(a, loc, 0.25, kb_conf)

    a.refused = True
    a.headline = "That point did not resolve to a council district"
    a.summary = (
        "It may sit outside Austin's full-purpose jurisdiction. Council representation is only "
        "defined inside the city limits."
    )
    _cite_core(loc, a)
    return _finalize(a, loc, 0.2, kb_conf)


# ----------------------------------------------------------- neighborhood ---
def build_neighborhood(
    question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float
) -> Answer:
    a = Answer(question=question, intent="neighborhood", headline="Neighborhood", summary="")
    add_kb_refs(question, a)
    if loc.neighborhood is None:
        a.refused = True
        a.headline = "That point is not inside an adopted neighborhood planning area"
        a.summary = (
            "Neighborhood planning areas cover part of Austin, not all of it. Outside an adopted "
            "NPA the citywide ordinances apply without an area plan on top."
        )
        return _finalize(a, loc, 0.3, kb_conf)
    ev = _evidence(loc.neighborhood, "neighborhoods")
    a.headline = "%s is in the %s neighborhood planning area" % (
        loc.display,
        ev.get("planning_area_name"),
    )
    a.summary = (
        "The city's neighborhood layer places this point in the %s neighborhood planning area, "
        "%s. An adopted NPA can carry its own plan with rules layered on top of citywide zoning."
        % (ev.get("planning_area_name"), str(ev.get("neighborhood_planning_status") or "").lower())
    )
    for label, key in (
        ("Neighborhood planning area", "planning_area_name"),
        ("Planning status", "neighborhood_planning_status"),
        ("Neighborhood number", "neighborhood_number"),
        ("Adopting ordinance", "ordinance_number"),
    ):
        if ev.get(key) not in (None, ""):
            a.facts.append(Fact(label, _fmt(ev[key]), dataset_key="neighborhoods"))
    cite_place(loc.neighborhood, a)
    _cite_core(loc, a)
    a.follow_ups = ["What is the zoning at %s?" % loc.display]
    return _finalize(a, loc, 0.85, kb_conf)


# ---------------------------------------------------------------- historic --
def build_historic(question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float) -> Answer:
    from .resolve import _extract_address_phrase, historic_at

    a = Answer(question=question, intent="historic", headline="Historic designation", summary="")
    add_kb_refs(question, a)

    # Historic designation is keyed on the address text, so even if the point
    # did not geocode we can still query the designation list directly.
    address = (loc.place.address if loc.place else "") or _extract_address_phrase(question) or ""
    if not address:
        a.refused = True
        a.headline = "I need an address to check historic designation"
        a.summary = (
            "Historic designation is recorded per property address, so I need the address before "
            "I can search the list. Drop a pin on the map or type one."
        )
        return _finalize(a, loc, 0.2, kb_conf)

    normalized = address.strip().upper()
    place = historic_at(address)
    cite_query(
        "historic",
        {
            "$select": "dpb,property,building_name,ordinance_link",
            "$where": "upper(property) = '%s'" % normalized.replace("'", "''"),
        },
        a,
        title="Historic designation lookup for %s" % address,
        count=1 if place else 0,
    )
    if place is None:
        a.refused = True
        a.headline = "No local historic designation on record for %s" % address
        a.summary = (
            "The city's historic properties list returned no record for that address, and the "
            "lookup is cited above so you can confirm it. A miss means no *local* designation on "
            "file - it says nothing about national or state listings, which live in other "
            "registers."
        )
        _cite_core(loc, a)
        return _finalize(a, loc, 0.5, kb_conf)

    ev = _evidence(place, "historic")
    a.headline = "%s is designated %s" % (address, ev.get("building_name") or "a historic property")
    a.summary = (
        "%s appears on the city's historic properties list as %s, designated under DPB record %s."
        % (address, ev.get("building_name"), ev.get("dpb"))
    )
    for label, key in (
        ("Historic name", "building_name"),
        ("Designation record", "dpb"),
        ("Designation ordinance", "ordinance_link"),
    ):
        if ev.get(key) not in (None, ""):
            a.facts.append(Fact(label, str(ev[key]), dataset_key="historic"))
    cite_place(place, a)
    _cite_core(loc, a)
    return _finalize(a, loc, 0.85, kb_conf)


# -------------------------------------------------------------- definition --
def build_definition(question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float) -> Answer:
    from . import retrieve as _retrieve

    a = Answer(question=question, intent="definition", headline="Definition", summary="")
    results = _retrieve.search(question, top_k=3)
    if not results:
        a.refused = True
        a.headline = "I do not have that in my verified knowledge base"
        a.summary = (
            "CivicLens only explains things it can point a source at. Ask me about a FEMA flood "
            "zone, a zoning category, a 311 service type, or where a rule is written."
        )
        return _finalize(a, loc, 0.2, kb_conf)

    doc, raw = results[0]
    conf = _retrieve.normalize_confidence(raw)
    if conf < 0.2:
        a.refused = True
        a.headline = "I could not match that to a verified source"
        a.summary = "My knowledge base is small and specific, and nothing in it matched that phrasing."
        a.follow_ups = ["What does FEMA flood zone AE mean?", "What does a planned unit development mean?"]
        return _finalize(a, loc, 0.15, kb_conf)

    a.headline = doc.title
    a.summary = doc.body
    for other, score in results[1:]:
        a.kb_refs.append(
            {
                "id": other.id,
                "title": other.title,
                "topic": other.topic,
                "source_label": other.source_label,
                "source_url": other.source_url,
                "score": round(score, 3),
            }
        )
    a.kb_refs.append(
        {
            "id": doc.id,
            "title": doc.title,
            "topic": doc.topic,
            "source_label": doc.source_label,
            "source_url": doc.source_url,
            "score": round(raw, 3),
        }
    )
    a.follow_ups = ["What can you answer about my property?"]
    return _finalize(a, loc, conf, kb_conf)


# ----------------------------------------------------------------- unknown --
def build_unknown(question: str, loc: ResolvedLocation, slots: Slots, kb_conf: float) -> Answer:
    a = Answer(question=question, intent="unknown", headline="I can't answer that from city data", summary="")
    a.refused = True
    a.summary = (
        "CivicLens answers questions it can ground in the city's own records: zoning, parcels, "
        "permits, 311 cases, flood zones, council districts and neighborhood plans. It refuses "
        "everything else rather than inventing something plausible.\n\n"
        "Try one of these, or give me an address and ask what you want to know about it."
    )
    a.follow_ups = [
        "Can I build a duplex at 1507 Bouldin Ave?",
        "Is 1507 Bouldin Ave in a floodplain?",
        "What permits have been issued at 1507 Bouldin Ave?",
        "Which districts get the most 311 requests?",
        "What does FEMA flood zone AE mean?",
    ]
    return _finalize(a, loc, 0.0, kb_conf)


# ----------------------------------------------------------------- facade --
BUILDERS = {
    "property_profile": build_property_profile,
    "zoning_feasibility": build_zoning_feasibility,
    "flood": build_flood,
    "permits": build_permits,
    "service_requests": build_service_requests,
    "analytics": build_analytics,
    "council": build_council,
    "neighborhood": build_neighborhood,
    "historic": build_historic,
    "definition": build_definition,
    "unknown": build_unknown,
}


def dispatch(intent: str, question: str, loc: ResolvedLocation, slots: Slots) -> Answer:
    builder = BUILDERS.get(intent, build_unknown)
    return builder(question, loc, slots, 0.0)
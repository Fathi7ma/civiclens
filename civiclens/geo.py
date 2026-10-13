"""Geometry helpers: point-in-polygon, distance, bounding boxes, geocoding.

The Austin open-data instance does not support ``within_circle`` or
``within_distance``, so CivicLens narrows spatially with ``within_box`` and
then does the precise geometry locally. That keeps every answer exact without
pulling in a compiled geometry dependency.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

EARTH_RADIUS_M = 6371008.8

Point = Tuple[float, float]  # (lon, lat)
BBox = Tuple[float, float, float, float]  # (min_lat, min_lon, max_lat, max_lon)


# --------------------------------------------------------------- geojson ---
def parse_geom(value: Any) -> Optional[Dict[str, Any]]:
    """Socrata returns geometries as dicts or as JSON strings depending on type."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        text = value.strip()
        if not text or text in ("null", ""):
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and parsed.get("type"):
            return parsed
    # Socrata's "location" columns arrive as {"latitude":..,"longitude":..}
    if isinstance(value, dict):
        return None
    return None


def geom_points(geom: Dict[str, Any]) -> Iterable[Tuple[float, float]]:
    """Yield every (lon, lat) vertex in a GeoJSON geometry."""
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point":
        if coords and len(coords) >= 2:
            yield (float(coords[0]), float(coords[1]))
    elif gtype in ("MultiPoint", "LineString"):
        for c in coords or []:
            if len(c) >= 2:
                yield (float(c[0]), float(c[1]))
    elif gtype in ("MultiLineString", "Polygon"):
        for ring in coords or []:
            for c in ring or []:
                if len(c) >= 2:
                    yield (float(c[0]), float(c[1]))
    elif gtype == "MultiPolygon":
        for poly in coords or []:
            for ring in poly or []:
                for c in ring or []:
                    if len(c) >= 2:
                        yield (float(c[0]), float(c[1]))
    elif gtype == "GeometryCollection":
        for sub in geom.get("geometries", []) or []:
            if isinstance(sub, dict):
                for pt in geom_points(sub):
                    yield pt


def geom_bbox(geom: Dict[str, Any]) -> Optional[BBox]:
    lons: List[float] = []
    lats: List[float] = []
    for lon, lat in geom_points(geom):
        lons.append(lon)
        lats.append(lat)
    if not lons:
        return None
    return (min(lats), min(lons), max(lats), max(lons))


def bbox_of(points: Iterable[Tuple[float, float]]) -> Optional[BBox]:
    lons = []
    lats = []
    for lon, lat in points:
        lons.append(lon)
        lats.append(lat)
    if not lons:
        return None
    return (min(lats), min(lons), max(lats), max(lons))


def pad_bbox(bbox: BBox, lat_pad: float, lon_pad: float) -> BBox:
    min_lat, min_lon, max_lat, max_lon = bbox
    return (min_lat - lat_pad, min_lon - lon_pad, max_lat + lat_pad, max_lon + lon_pad)


def bbox_for_radius(lat: float, lon: float, meters: float) -> BBox:
    """Bounding box that fully encloses a circle of ``meters`` around a point."""
    # A hair of slack so a point exactly on the circle is never clipped by
    # floating-point rounding in the server-side box filter.
    slack = 1.0000005
    d_lat = math.degrees(meters * slack / EARTH_RADIUS_M)
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    d_lon = math.degrees(meters * slack / (EARTH_RADIUS_M * cos_lat))
    return (lat - d_lat, lon - d_lon, lat + d_lat, lon + d_lon)


def lonlat_bbox_to_where(bbox: BBox, lat_field: str, lon_field: str) -> str:
    min_lat, min_lon, max_lat, max_lon = bbox
    return (
        "{lat} > {mlat} and {lat} < {xlat} and {lon} > {mlon} and {lon} < {xlon}".format(
            lat=lat_field,
            lon=lon_field,
            mlat=min_lat,
            xlat=max_lat,
            mlon=min_lon,
            xlon=max_lon,
        )
    )


def point_str(lon: float, lat: float) -> str:
    return "POINT (%s %s)" % (lon, lat)


# --------------------------------------------------------- point in polygon -
def _point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    """Ray-casting algorithm. ring is [(lon, lat), ...]."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > lat) != (y2 > lat):
            x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_at:
                inside = not inside
    return inside


def point_in_geom(lon: float, lat: float, geom: Optional[Dict[str, Any]]) -> bool:
    """Point-in-polygon with correct handling of GeoJSON holes."""
    if not geom:
        return False
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon":
        polys = [coords]
    elif gtype == "MultiPolygon":
        polys = coords
    elif gtype in ("Point", "MultiPoint"):
        for c in coords:
            if abs(float(c[0]) - lon) < 1e-7 and abs(float(c[1]) - lat) < 1e-7:
                return True
        return False
    else:
        # Approximate: any containing linear ring counts (lines/collections).
        for lon_v, lat_v in geom_points(geom):
            if abs(lon_v - lon) < 1e-7 and abs(lat_v - lat) < 1e-7:
                return True
        return False

    for poly in polys:
        if not poly:
            continue
        if not _point_in_ring(lon, lat, poly[0]):
            continue
        in_hole = False
        for hole in poly[1:]:
            if _point_in_ring(lon, lat, hole):
                in_hole = True
                break
        if not in_hole:
            return True
    return False


def nearest_geom(
    lon: float, lat: float, rows: Iterable[Dict[str, Any]], geom_field: str = "the_geom"
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], float]]:
    """Return (row, geom, distance_m) for the closest row containing the point,
    falling back to the nearest row overall."""
    best_contains: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None
    best_nearest: Optional[Tuple[Dict[str, Any], Dict[str, Any], float]] = None
    for row in rows:
        geom = parse_geom(row.get(geom_field))
        if not geom:
            continue
        if point_in_geom(lon, lat, geom):
            if best_contains is None:
                best_contains = (row, geom)
            continue
        bb = geom_bbox(geom)
        if not bb:
            continue
        pts = list(geom_points(geom))
        if not pts:
            continue
        d = haversine(lat, lon, pts[0][1], pts[0][0])
        for p_lon, p_lat in pts[1:]:
            d = min(d, haversine(lat, lon, p_lat, p_lon))
        if best_nearest is None or d < best_nearest[2]:
            best_nearest = (row, geom, d)
    if best_contains:
        return (best_contains[0], best_contains[1], 0.0)
    return best_nearest


def within_radius(
    rows: Iterable[Dict[str, Any]], lat: float, lon: float, meters: float, lat_field: str, lon_field: str
) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        try:
            r_lat = float(row[lat_field])
            r_lon = float(row[lon_field])
        except (KeyError, TypeError, ValueError):
            continue
        if haversine(lat, lon, r_lat, r_lon) <= meters:
            out.append(row)
    return out


# ---------------------------------------------------------------- distance -
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- formatting
def sqft_to_acres(sqft: Optional[float]) -> Optional[float]:
    if sqft is None:
        return None
    try:
        return round(float(sqft) / 43560.0, 2)
    except (TypeError, ValueError):
        return None


def humanize_distance(meters: float) -> str:
    if meters is None:
        return "unknown distance"
    if meters < 950:
        return "%d m (%.0f ft)" % (round(meters), meters * 3.28084)
    return "%.1f km (%.1f mi)" % (meters / 1000.0, meters / 1609.344)


# --------------------------------------------------------------- geocoding --
_STREET_SUFFIXES = (
    "street st road rd avenue ave boulevard blvd drive dr lane ln court ct circle cir "
    "way terrace ter place pl parkway pkwy highway hwy loop trail trl path pathway close "
    "crescent cres square sq row walk alley"
).split()

_DIRECTIONS = (
    "north south east west northeast northwest southeast southwest n s e w ne nw se sw"
).split()


#: Austin's geocoded addresses abbreviate directions ("S 1ST ST"), keep
#: ordinals in digit form, and include fractional house numbers ("2404 1/2").
_DIRECTION_TO_ABBR = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
_ABBR_TO_DIRECTION = {v: k for k, v in _DIRECTION_TO_ABBR.items()}

_SUFFIX_TO_ABBR = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR",
    "BOULEVARD": "BLVD", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR",
    "TERRACE": "TER", "PLACE": "PL", "PARKWAY": "PKWY", "HIGHWAY": "HWY",
    "TRAIL": "TRL", "LOOP": "LOOP", "SQUARE": "SQ", "CRESCENT": "CRES",
    "ALLEY": "ALY", "WALK": "WALK", "PATH": "PATH", "ROW": "ROW",
}

_UNIT_TO_ABBR = {
    "APARTMENT": "APT", "APART": "APT", "SUITE": "STE", "BUILDING": "BLDG",
    "OFFICE": "OFC", "TRAILER": "TRLR", "UNIT": "UNIT", "REAR": "REAR",
    "FRONT": "FRNT", "LOWER": "LOWER", "UPPER": "UPR",
}

_ORDINALS = {
    "FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH",
    "FIFTH": "5TH", "SIXTH": "6TH", "SEVENTH": "7TH", "EIGHTH": "8TH",
    "NINTH": "9TH", "TENTH": "10TH",
}

_TOKEN_RE = re.compile(r"[A-Z]+|\d+(?:/\d+)?|\d+(?:ST|ND|RD|TH)")


def normalize_address(text: str) -> str:
    """Normalise a typed address into the form the city's data uses.

    Example: ``"1200 south lamar avenue"`` -> ``"1200 S LAMAR BLVD"``-style
    canonical text. The city's own data is not perfectly consistent (both
    ``W 2ND ST`` and ``WEST AVE`` occur), so this is a best-effort canonical
    form and the geocoder applies several progressively looser passes.
    """
    s = (text or "").upper()
    s = re.sub(r"[^A-Z0-9/\-# ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    tokens: List[str] = []
    for token in s.split(" "):
        if not token:
            continue
        if token in _ORDINALS:
            tokens.append(_ORDINALS[token])
        elif token in _SUFFIX_TO_ABBR:
            tokens.append(_SUFFIX_TO_ABBR[token])
        elif token in _UNIT_TO_ABBR:
            tokens.append(_UNIT_TO_ABBR[token])
        elif token in _DIRECTION_TO_ABBR:
            tokens.append(_DIRECTION_TO_ABBR[token])
        else:
            tokens.append(token)
    return " ".join(tokens).strip()


def address_variants(normalized: str) -> List[str]:
    """Plausible spellings of a normalised address, most precise first.

    Needed because the city records both ``W 2ND ST`` and ``WEST AVE`` for the
    same kind of thing; trying each variant turns a near-miss into a hit.
    """
    variants = [normalized]
    swapped = " ".join(
        _ABBR_TO_DIRECTION.get(t, t) for t in normalized.split(" ")
    )
    if swapped != normalized:
        variants.append(swapped)
    # Mixed form, e.g. "W COURTYARD DR" vs "WEST COURTYARD DR".
    for i, token in enumerate(normalized.split(" ")):
        long_form = _ABBR_TO_DIRECTION.get(token)
        if long_form:
            parts = normalized.split(" ")
            parts[i] = long_form
            variants.append(" ".join(parts))
    seen = set()
    unique = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


def address_like(text: str) -> bool:
    """Heuristic: does this string look like a street address rather than prose?"""
    if not text:
        return False
    t = text.strip().upper()
    has_number = bool(re.search(r"\b\d{1,6}\b", t))
    words = re.findall(r"[A-Z]+", t)
    if not has_number or len(words) < 2:
        return False
    suffixes = set(_STREET_SUFFIXES)
    directions = set(_DIRECTIONS)
    # Compare case-insensitively - the word list is extracted from uppercased
    # text, so it has to be lowered before matching the vocabularies.
    lowered = [
        w.lower()
        for w in words
        if w.lower() not in ("apt", "ste", "unit", "the", "at", "of", "is", "austin", "tx")
    ]
    if any(w in suffixes for w in lowered):
        return True
    if len(t.split()) <= 6 and any(w in directions for w in lowered):
        return True
    return False


def parse_house_number(text: str) -> Optional[str]:
    m = re.search(r"\b(\d{1,6})(?:\s*(?:st|nd|rd|th))?\b", text or "")
    return m.group(1) if m else None
"""Knowledge base.

Central design decision
-----------------------
CivicLens does **not** encode legal dimensional standards (setbacks, FAR, lot
coverage, height limits) as facts it recites from memory. Those numbers vary
by district, overlay and amendments, and a stale constant is worse than no
answer. Instead the knowledge base stores:

1. Definitions that are stable and verifiable (FEMA zone meanings, what a
   district category is, how to look up a record).
2. *Navigation* - the exact authoritative code section a person must read.

This is what lets the engine refuse instead of hallucinating, and it is the
single most important differentiator against a general-purpose LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import CITY


@dataclass(frozen=True)
class KBDoc:
    id: str
    title: str
    topic: str
    body: str
    source_label: str
    source_url: str
    dataset_hint: Optional[str] = None

    @property
    def text(self) -> str:
        return "%s %s %s" % (self.title, self.topic, self.body)


# --------------------------------------------------------------- FEMA zones -
# Codes as they literally appear in the Greater Austin FEMA Floodplain dataset.
FEMA_ZONES: Dict[str, Dict[str, str]] = {
    "A": {
        "label": "Zone A - 1% annual chance (100-year) flood",
        "risk": "high",
        "meaning": (
            "Mandatory 1% annual chance flood zone with no base flood elevation determined. "
            "Expect significant standing water in a 100-year storm."
        ),
        "note": "Insurance is required by lenders for any federally related loan on this parcel.",
    },
    "AE": {
        "label": "Zone AE - 1% annual chance flood with base flood elevation",
        "risk": "high",
        "meaning": (
            "1% annual chance (100-year) flood zone where a base flood elevation has been "
            "calculated. Includes the regulatory floodway along creeks."
        ),
        "note": "The base flood elevation on the effective map is the legal floor for design.",
    },
    "AO": {
        "label": "Zone AO - sheet flow, 1 to 3 feet deep",
        "risk": "high",
        "meaning": "Shallow sheet-flow flooding, 1-3 feet deep, velocity not considered.",
        "note": "Common in wide flat areas away from the main creek channel.",
    },
    "AH": {
        "label": "Zone AH - shallow water, 1 to 3 feet deep",
        "risk": "high",
        "meaning": "1% annual chance flooding in an area of shallow water with known depths.",
        "note": "Depths are typically one to three feet.",
    },
    ".2 PCT ANNUAL CHANCE FLOOD HAZARD": {
        "label": "Zone X (shaded) - 0.2% annual chance (500-year) flood",
        "risk": "moderate",
        "meaning": (
            "Outside the Special Flood Hazard Area. There is a 0.2% annual chance - a "
            "500-year event - of a flood of this magnitude."
        ),
        "note": (
            "Being outside Zone A/AE does not mean risk-free. Roughly half of all flood "
            "claims come from X-shaded parcels."
        ),
    },
    "X PROTECTED BY LEVEE": {
        "label": "Zone X - protected by a levee",
        "risk": "low",
        "meaning": "Leveed area where 0.2% annual chance flooding is reduced by the levee system.",
        "note": "This classification assumes the levee keeps operating as designed.",
    },
    "X": {
        "label": "Zone X - 0.2% annual chance (500-year) flood",
        "risk": "moderate",
        "meaning": "Not in the 1% annual chance flood zone; a 500-year event can still flood it.",
        "note": None,
    },
    "V": {
        "label": "Zone V - coastal high hazard",
        "risk": "extreme",
        "meaning": "Coastal high hazard area subject to wave action.",
        "note": "Not present in Austin; FEMA defines it for coastal counties.",
    },
    "AREA NOT INCLUDED": {
        "label": "Not included in the FEMA study",
        "risk": "unknown",
        "meaning": "This location falls outside the mapped study area.",
        "note": (
            "Absence from a FEMA panel is not a flood-safety determination. Check whether "
            "the property is in an unstudied area downstream of a dam."
        ),
    },
}

RISK_ORDER = {"extreme": 0, "high": 1, "moderate": 2, "low": 3, "unknown": 4}

RISK_TONE = {
    "extreme": "critical",
    "high": "danger",
    "moderate": "warning",
    "low": "ok",
    "unknown": "muted",
}


def flood_zone_info(code: Optional[str]) -> Optional[Dict[str, Optional[str]]]:
    """Look up a FEMA zone. Returns None for codes we cannot speak to."""
    if not code:
        return None
    key = code.strip().upper()
    if key in FEMA_ZONES:
        info = FEMA_ZONES[key]
        return {"code": key, "risk": info["risk"], **info}
    # Austin's dataset also carries unstudied/unmapped strings.
    if "NOT INCLUDED" in key or "UNMAPPED" in key:
        info = FEMA_ZONES["AREA NOT INCLUDED"]
        return {"code": key, "risk": "unknown", **info}
    if "500" in key or ".2 PCT" in key:
        info = FEMA_ZONES[".2 PCT ANNUAL CHANCE FLOOD HAZARD"]
        return {"code": key, "risk": "moderate", **info}
    return None


# --------------------------------------------------------- zoning categories
# Descriptions of *categories*, which are stable. District-specific numeric
# standards are intentionally absent - see the module docstring.
ZONING_CATEGORY_NOTES: Dict[str, Dict[str, str]] = {
    "single family residence": {
        "summary": "One dwelling per lot.",
        "what": (
            "A traditional single-family district. The base rules of that district govern "
            "what you may build; accessory dwelling units are generally permitted on a "
            "portion of the lot where the district allows them."
        ),
    },
    "single family standard lot": {
        "summary": "One dwelling on a standard-sized lot.",
        "what": (
            "The workhorse residential district. One home per lot, with dimensional "
            "standards set by the district itself."
        ),
    },
    "single family small lot": {
        "summary": "One dwelling on a smaller lot.",
        "what": (
            "Single-family zoning on a lot smaller than the traditional standard lot, so the "
            "dimensional standards are tighter to keep the same house-to-lot rhythm."
        ),
    },
    "single family large lot": {
        "summary": "One dwelling on a larger lot.",
        "what": "Single-family zoning where the district targets larger lots and setbacks.",
    },
    "single family estate district": {
        "summary": "Large-lot residential with a strong acreage floor.",
        "what": (
            "Estate-scale districts require a much larger lot than the single-family "
            "districts that dominate the city."
        ),
    },
    "planned unit development": {
        "summary": "PUD - the district's own approved plan controls.",
        "what": (
            "A planned unit development is governed by its recorded plan and ordinance, "
            "which can and does override the base dimensional table. To know the rules on "
            "PUD land you must read the plan document for that specific district."
        ),
    },
    "duplex": {
        "summary": "Two dwellings on one lot.",
        "what": "Duplex districts allow two primary dwellings per lot under the district rules.",
    },
    "condominium": {
        "summary": "Condominium - unit ownership in a shared building envelope.",
        "what": "Multi-family unit ownership; the condo documents add a second layer of rules.",
    },
    "townhouse": {
        "summary": "Row or townhouse units sharing party walls.",
        "what": "Townhouse districts allow attached or closely spaced dwellings per lot.",
    },
    "zero lot line": {
        "summary": "Dwellings built to the lot line.",
        "what": "One side wall sits on the property line, so side setbacks are minimal.",
    },
    "multi-family": {
        "summary": "Multiple dwellings on one lot.",
        "what": "Multi-family districts permit several primary dwellings per lot under district rules.",
    },
    "apartments": {
        "summary": "Apartment district.",
        "what": "Multi-family residential, with density governed by the district table.",
    },
    "community commercial": {
        "summary": "Neighbourhood-serving commercial.",
        "what": (
            "Commercial district for everyday needs close to homes. Hours and operations are "
            "typically regulated by use, and some categories overlap with residential."
        ),
    },
    "neighborhood commercial": {
        "summary": "Small-scale neighbourhood commercial.",
        "what": "Commercial services serving an immediate neighbourhood rather than a region.",
    },
    "commercial": {
        "summary": "General commercial.",
        "what": "General commercial district; permitted uses and conditions come from the district.",
    },
    "central business district": {
        "summary": "Downtown core.",
        "what": "The highest-intensity commercial core of the city.",
    },
    "light commercial": {
        "summary": "Light commercial and service.",
        "what": "Lower-intensity commercial, often with showrooms, service and light retail.",
    },
    "industrial": {
        "summary": "Industrial.",
        "what": "Industrial district; noise, nuisance and access rules are typically stricter.",
    },
    "major arterial": {
        "summary": "Major arterial corridor.",
        "what": "Frontage on a major arterial, which usually triggers additional requirements.",
    },
    "office": {
        "summary": "Office.",
        "what": "Office district.",
    },
    "mixed use": {
        "summary": "Mixed residential and commercial.",
        "what": "A district where several use categories are permitted in the same building.",
    },
    "university": {
        "summary": "University / institutional.",
        "what": "Institutional district tied to education or civic uses.",
    },
    "institutional": {
        "summary": "Institutional.",
        "what": "Civic, educational, religious or healthcare uses.",
    },
    "open space": {
        "summary": "Open space.",
        "what": "Parks, preserves and undeveloped open land.",
    },
    "public": {
        "summary": "Public / civic use.",
        "what": "Land owned or used for public purposes.",
    },
}


def zoning_category_note(category: Optional[str]) -> Optional[Dict[str, str]]:
    if not category:
        return None
    key = category.strip().lower()
    if key in ZONING_CATEGORY_NOTES:
        return ZONING_CATEGORY_NOTES[key]
    # Suffix match: "Single Family Residence District" etc.
    for known, note in ZONING_CATEGORY_NOTES.items():
        if known in key:
            return note
    return None


# ------------------------------------------------------------- code routing -
_CODE = CITY["code"]  # type: ignore[index]

CODE_ROUTES: Dict[str, Dict[str, str]] = {
    "zoning_dimensional": {
        "topic": "zoning dimensional standards",
        "where": "Development Code, Chapter 25-2 (Zoning) - establishment of district regulations",
        "url": _CODE["development_code"],
        "why": (
            "Chapter 25-2 sets up each district. The dimensional table for your particular "
            "district (minimum lot size, setbacks, maximum height, floor-area ratio) is in "
            "that chapter, and overlays such as historic district, corridor, transit or "
            "floodplain overlays can add further requirements on top of it."
        ),
    },
    "zoning_overlay": {
        "topic": "zoning overlays",
        "where": "Development Code, Chapter 25-2 - overlay district regulations",
        "url": _CODE["development_code"],
        "why": (
            "Overlays such as HY, H, C, N, P or F change what the base district allows. "
            "Whether one applies to a parcel is recorded on the zoning determination itself."
        ),
    },
    "permits": {
        "topic": "building permit process",
        "where": "Austin Development Services - permit search",
        "url": _CODE["permit_search"],
        "why": (
            "A permit record shows what the city *did* approve, which is not the same as what "
            "the code *allows* today. For what is allowed now, read the current code."
        ),
    },
    "service_requests": {
        "topic": "311 service requests",
        "where": "City of Austin 311",
        "url": _CODE["311_search"],
        "why": "311 cases are the city's public record of what residents reported.",
    },
    "floodplain": {
        "topic": "flood maps and insurance",
        "where": "FEMA Flood Map Service Center",
        "url": _CODE["fema_firm"],
        "why": (
            "Only FEMA's effective maps are authoritative. The panel cited in the answer is "
            "the one a lender and an insurer will use."
        ),
    },
    "council": {
        "topic": "city council districts and representation",
        "where": "City of Austin council district lookup",
        "url": "https://www.austintexas.gov/city-council",
        "why": "District boundaries are redrawn by ordinance; verify the current map.",
    },
    "tax": {
        "topic": "property tax rates and appraisals",
        "where": "Travis County Tax Office / Central Appraisal District",
        "url": "https://www.traviscountytx.gov/tax_officer/",
        "why": (
            "CivicLens does not compute tax liability. Appraisals live with the Central "
            "Appraisal District and rates are set locally each year."
        ),
    },
    "history": {
        "topic": "historic designation",
        "where": "City of Austin historic designation records",
        "url": "https://www.austintexas.gov/page/historic-preservation",
        "why": "Historic designation attaches a review process to property changes.",
    },
    "neighborhood": {
        "topic": "neighborhood planning areas",
        "where": "City of Austin neighborhood planning areas",
        "url": "https://www.austintexas.gov/page/neighborhood-planning-commission",
        "why": (
            "A neighborhood planning area may have its own adopted plan with additional rules "
            "on top of citywide zoning."
        ),
    },
}


# ------------------------------------------------------- 311 service topics -
SERVICE_GLOSSARY: Dict[str, str] = {
    "pothole": "Road surface damage; repaired by Public Works street maintenance.",
    "graffiti": "Unauthorised markings; removal is handled by a city contractor.",
    "code violation": "An enforcement case, e.g. illegal structure or occupancy.",
    "bulky waste": "Large discarded items, usually scheduled by collection area and day.",
    "tree": "City-maintained tree issues: pruning, removal, planting, storm damage.",
    "water main": "Drinking-water network: leaks, breaks, low pressure.",
    "traffic signal": "Signal timing, malfunction and knocked-down poles.",
    "signage": "Illegal or unsafe signs and sign posts.",
    "missed collection": "Trash or recycling that was not collected on schedule.",
    "streetlight": "Public lighting outages and damaged poles.",
    "sidewalk": "Sidewalk obstruction, construction damage and trip hazards.",
    "storm drain": "Blocked or damaged drainage infrastructure.",
    "abandoned property": "Property reported as a health or safety hazard.",
    "dangerous/vicious dog": "Animal Services investigations.",
    "animal": "Stray or nuisance animal reports.",
    "loose dog": "Reported loose or unrestrained dog.",
    "illegal dumping": "Waste dumped outside authorised facilities.",
    "sewer backup": "Sanitary sewer blockages and overflows.",
    "weeds": "Overgrown weeds or vegetation on private property.",
}


# --------------------------------------------------------------- KB corpus --
def _docs() -> List[KBDoc]:
    docs: List[KBDoc] = []

    for code, info in FEMA_ZONES.items():
        body = "%s %s %s" % (info["label"], info["meaning"], info.get("note") or "")
        docs.append(
            KBDoc(
                id="fema:%s" % code,
                title="FEMA flood zone %s" % code,
                topic="flood floodplain FEMA insurance zone risk",
                body=body,
                source_label="FEMA Flood Map Service Center",
                source_url=_CODE["fema_firm"],
                dataset_hint="floodplain",
            )
        )

    for name, note in ZONING_CATEGORY_NOTES.items():
        docs.append(
            KBDoc(
                id="zcat:%s" % name,
                title="Zoning category: %s" % name,
                topic="zoning district category land use residential commercial what does it mean",
                body="%s %s" % (note["summary"], note["what"]),
                source_label="City of Austin Development Code",
                source_url=_CODE["development_code"],
                dataset_hint="zoning",
            )
        )

    for key, route in CODE_ROUTES.items():
        docs.append(
            KBDoc(
                id="route:%s" % key,
                title="Where the rules for %s are written" % route["topic"],
                topic="%s code regulation where to look official" % route["topic"],
                body="%s %s" % (route["where"], route["why"]),
                source_label=route["where"],
                source_url=route["url"],
            )
        )

    for term, text in SERVICE_GLOSSARY.items():
        docs.append(
            KBDoc(
                id="svc:%s" % term,
                title="311 service type: %s" % term,
                topic="311 service request city report complaint case",
                body=text,
                source_label="City of Austin 311",
                source_url=_CODE["311_search"],
                dataset_hint="service_requests",
            )
        )

    docs.append(
        KBDoc(
            id="method:citation",
            title="How CivicLens proves an answer",
            topic="how does this work citation evidence verify methodology trust",
            body=(
                "Every fact in an answer is produced by a live query against the city's own "
                "open-data portal, and the answer carries the exact reproducible query URL. "
                "Click it and you run the same query yourself. If CivicLens cannot find "
                "supporting data it says so instead of estimating."
            ),
            source_label="CivicLens method",
            source_url="https://github.com/topics/civiclens",
        )
    )

    docs.append(
        KBDoc(
            id="method:dimensionlimit",
            title="Why CivicLens will not recite setback or height numbers",
            topic="setback height FAR floor area ratio limits why not answer dimensional",
            body=(
                "Dimensional standards change with district, overlay and amendment, and they "
                "are enforced by a real document rather than a chatbot. CivicLens identifies "
                "the district, tells you which code chapter governs it, and links the "
                "authoritative section, rather than guessing a number you might build on."
            ),
            source_label="Development Code, Chapter 25-2",
            source_url=_CODE["development_code"],
        )
    )

    return docs


DOCS: List[KBDoc] = _docs()
DOC_BY_ID: Dict[str, KBDoc] = {d.id: d for d in DOCS}


def routes_for(topic: str) -> List[Dict[str, str]]:
    return [v for v in CODE_ROUTES.values() if topic in v["topic"]]
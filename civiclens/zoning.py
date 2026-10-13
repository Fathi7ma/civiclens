"""Zoning designation interpretation.

Austin zoning designations look like ``SF-3-NP`` or ``GR-V-CO-NP``: a base
district, an optional numeric variant, then overlay suffixes.

What this module deliberately does **not** do is state dimensional standards.
It reads the *structure* of a designation (which family the base district is
in, which overlays are attached) and routes the user to the governing code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .kb import zoning_category_note

_DESIG_RE = re.compile(r"^([A-Z]+(?:-[A-Z]+)*?)(?:-(\d+))?(?:-(.+))?$")

#: Base districts whose name we are confident about, grouped by what they allow
#: at the *category* level. This is not a determination - it is a router.
BASE_DISTRICT_FAMILY: Dict[str, Dict[str, str]] = {
    "SF": {
        "family": "single family",
        "label": "Single Family residence district",
        "summary": "One dwelling per lot.",
    },
    "ER": {
        "family": "estate residential",
        "label": "Estate Residential district",
        "summary": "One dwelling on a large lot.",
    },
    "MF": {
        "family": "multi family",
        "label": "Multi-Family district",
        "summary": "Several dwellings per lot, density set by the district table.",
    },
    "MU": {
        "family": "mixed use",
        "label": "Mixed Use district",
        "summary": "Residential and commercial uses permitted in the same building.",
    },
    "GR": {
        "family": "commercial",
        "label": "General Retail Services district",
        "summary": "Commercial uses; some GR subdistricts also allow residential above ground floor.",
    },
    "CS": {
        "family": "commercial",
        "label": "Commercial Services district",
        "summary": "Neighbourhood-scale commercial services.",
    },
    "LO": {
        "family": "office",
        "label": "Local Office district",
        "summary": "Low-intensity office and services.",
    },
    "B": {
        "family": "commercial",
        "label": "Business district",
        "summary": "Commercial and office uses.",
    },
    "C": {
        "family": "commercial",
        "label": "Commercial district",
        "summary": "Commercial uses; C-1 and C-2 differ in intensity.",
    },
    "IND": {
        "family": "industrial",
        "label": "Industrial district",
        "summary": "Industrial and related uses.",
    },
    "PUD": {
        "family": "planned unit development",
        "label": "Planned Unit Development",
        "summary": "Governed by its own recorded plan and adopting ordinance.",
    },
    "TOD": {
        "family": "transit oriented development",
        "label": "Transit Oriented Development district",
        "summary": "Uses and density shaped by proximity to transit.",
    },
}

#: Common overlay suffixes. Descriptions are intentionally high level.
OVERLAY_SUFFIXES: Dict[str, str] = {
    "NP": "Neighborhood Planning Area overlay - an adopted neighbourhood plan adds rules.",
    "CO": "Conditional Overlay - conditional use rules apply on top of the base district.",
    "DA": "Downtown Austin Overlay.",
    "CC": "Convention Center Area overlay.",
    "T": "Townhouse overlay.",
    "H": "Historic district overlay - changes to a building require historic review.",
    "HC": "Historic district overlay (commercial).",
    "HD": "Historic district overlay (downtown).",
    "HDA": "Downtown Historic District overlay.",
    "PD": "Planned Development district overlay.",
    "U": "University district overlay.",
    "P": "Plaza / pedestrian-oriented overlay.",
    "B": "Bonus density / building height overlay (use with caution - meaning varies by district).",
}

#: Which base families allow a second dwelling as a primary structure.
DUPLEX_BY_FAMILY = {
    "single family": "no",
    "estate residential": "no",
    "multi family": "likely yes, subject to the district table",
    "mixed use": "likely yes, subject to the district table",
    "commercial": "depends on the district; residential use is often conditional",
    "office": "depends on the district",
    "industrial": "no",
    "planned unit development": "governed by the PUD plan document, not the base district",
    "transit oriented development": "depends on the district table",
}


@dataclass
class ZoningReading:
    designation: str
    base: Optional[str] = None
    variant: Optional[str] = None
    suffixes: List[str] = field(default_factory=list)
    family: Optional[str] = None
    family_label: Optional[str] = None
    family_summary: Optional[str] = None
    category: Optional[str] = None
    category_summary: Optional[str] = None
    category_note: Optional[Dict[str, str]] = None
    overlay_notes: List[Dict[str, str]] = field(default_factory=list)
    unknown_suffixes: List[str] = field(default_factory=list)

    @property
    def is_residential(self) -> bool:
        return self.family in {"single family", "estate residential", "multi family", "mixed use"}

    def duplex_guidance(self) -> Optional[str]:
        if not self.family:
            return None
        return DUPLEX_BY_FAMILY.get(self.family)

    def to_dict(self) -> Dict[str, object]:
        return {
            "designation": self.designation,
            "base": self.base,
            "variant": self.variant,
            "suffixes": self.suffixes,
            "family": self.family,
            "family_label": self.family_label,
            "family_summary": self.family_summary,
            "category": self.category,
            "category_summary": self.category_summary,
            "overlay_notes": self.overlay_notes,
            "unknown_suffixes": self.unknown_suffixes,
        }


def read_designation(
    designation: Optional[str], category: Optional[str] = None
) -> Optional[ZoningReading]:
    """Parse and annotate a zoning designation without inventing standards."""
    if not designation:
        return None
    raw = designation.strip().upper()
    reading = ZoningReading(designation=raw, category=category)

    # Split on dashes; classify each token.
    tokens = [t for t in raw.split("-") if t]
    head = tokens[0] if tokens else ""

    m = _DESIG_RE.match(raw)
    if m:
        reading.base = m.group(1)
        reading.variant = m.group(2)
        rest = m.group(3) or ""
        reading.suffixes = [s for s in rest.split("-") if s]
    else:
        reading.base = head
        reading.suffixes = tokens[1:]

    if reading.base:
        info = BASE_DISTRICT_FAMILY.get(reading.base)
        if info:
            reading.family = info["family"]
            reading.family_label = info["label"]
            reading.family_summary = info["summary"]

    if category:
        note = zoning_category_note(category)
        if note:
            reading.category_summary = note["summary"]
            reading.category_note = note
        else:
            reading.category_summary = category

    for suffix in reading.suffixes:
        desc = OVERLAY_SUFFIXES.get(suffix)
        if desc:
            reading.overlay_notes.append({"suffix": suffix, "meaning": desc})
        else:
            reading.unknown_suffixes.append(suffix)

    return reading
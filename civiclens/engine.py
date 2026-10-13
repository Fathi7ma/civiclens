"""The CivicLens answer engine.

Flow
----
    question -> intent + slots -> location resolution -> typed data queries
             -> structured answer -> citations + confidence

Every branch follows the same discipline:

* **Cite or omit.** A fact appears only if a query returned it, and it carries
  the reproducible query URL that produced it.
* **Refuse rather than guess.** If the datasets cannot answer the question the
  engine says so, names the gap, and points at the authoritative source.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import geo, retrieve
from .config import CITY, confidence_threshold, dataset
from .resolve import ResolvedLocation
from .socrata import get_client

log = logging.getLogger("civiclens.engine")


# ------------------------------------------------------------------- models -
@dataclass
class Fact:
    label: str
    value: Any
    note: Optional[str] = None
    dataset_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "note": self.note,
            "dataset_key": self.dataset_key,
        }


@dataclass
class Citation:
    dataset_key: str
    dataset_id: str
    dataset_name: str
    record_id: str
    query_url: str
    portal_page: str
    title: str
    detail: Dict[str, Any] = field(default_factory=dict)
    count: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dataset_key": self.dataset_key,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "record_id": self.record_id,
            "query_url": self.query_url,
            "portal_page": self.portal_page,
            "title": self.title,
            "detail": self.detail,
            "count": self.count,
        }


@dataclass
class Answer:
    question: str
    intent: str
    headline: str
    summary: str
    facts: List[Fact] = field(default_factory=list)
    citations: List[Citation] = field(default_factory=list)
    map: Optional[Dict[str, Any]] = None
    confidence: float = 0.0
    confidence_label: str = "low"
    refused: bool = False
    caveats: List[str] = field(default_factory=list)
    data_gaps: List[str] = field(default_factory=list)
    follow_ups: List[str] = field(default_factory=list)
    kb_refs: List[Dict[str, str]] = field(default_factory=list)
    location: Optional[Dict[str, Any]] = None
    elapsed_ms: int = 0

    def add_citation(self, citation: Citation) -> None:
        if citation not in self.citations:
            self.citations.append(citation)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "intent": self.intent,
            "headline": self.headline,
            "summary": self.summary,
            "facts": [f.to_dict() for f in self.facts],
            "citations": [c.to_dict() for c in self.citations],
            "map": self.map,
            "confidence": self.confidence,
            "confidence_label": self.confidence_label,
            "refused": self.refused,
            "caveats": self.caveats,
            "data_gaps": self.data_gaps,
            "follow_ups": self.follow_ups,
            "kb_refs": self.kb_refs,
            "location": self.location,
            "elapsed_ms": self.elapsed_ms,
            "city": {"name": CITY["name"], "state": CITY["state"]},
        }


def _confidence_label(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


# ------------------------------------------------------------------- intents -
INTENT_RULES: List[Tuple[str, Dict[str, float]]] = [
    (
        "flood",
        {
            "flood": 3.0, "floodplain": 3.5, "inundat": 2.0, "insurance": 1.2,
            "zone ae": 3.0, "zone a": 2.0, "500 year": 2.0, "100 year": 2.0,
            "base flood": 3.0, "firm": 2.0, "panel": 1.0, "creek rises": 1.5,
            "drown": 1.5, "water": 0.6,
        },
    ),
    (
        "historic",
        {
            "historic": 3.0, "landmark": 2.5, "designated": 1.5, "preservation": 2.0,
            "heritage": 2.0, "national register": 2.5,
        },
    ),
    (
        "service_requests",
        {
            "311": 3.5, "service request": 3.5, "pothole": 3.0, "complaint": 2.0,
            "graffiti": 2.5, "trash": 1.8, "garbage": 1.8, "streetlight": 2.2,
            "traffic signal": 2.2, "bulky": 2.0, "missed collection": 2.5,
            "reported": 1.2, "reported issue": 1.5, "open request": 2.0,
            "who handles": 1.5, "unresolved": 2.0, "how long": 1.2,
        },
    ),
    (
        "permits",
        {
            "permit": 3.0, "permits": 3.0, "what was built": 3.0, "construction": 2.0,
            "remodel": 2.0, "renovation": 2.0, "contractor": 1.8, "built": 1.2,
            "approved work": 2.5, "building record": 2.0,
        },
    ),
    (
        "council",
        {
            "council": 3.0, "councilmember": 3.0, "council member": 3.0,
            "representative": 2.0, "who represents": 3.0, "commissioner": 2.5,
            "district": 1.2, "ward": 2.5, "my district": 3.0,
        },
    ),
    (
        "zoning_feasibility",
        {
            "can i build": 3.5, "can i put": 3.0, "can i": 1.6, "am i allowed": 3.5,
            "allowed to": 3.0, "zoning": 2.5, "rezone": 3.0, "setback": 2.5,
            "duplex": 3.0, "accessory dwelling": 3.0, "adu": 3.0, "granny flat": 2.8,
            "subdivide": 3.0, "subdivid": 3.0, "build": 1.5, "construct": 1.5,
            "second unit": 2.8, "what can i build": 3.5, "height limit": 2.5,
            "floor area ratio": 2.5, "farr": 2.5, "lot coverage": 2.5, "zoned": 2.0,
            "pud": 2.0, "short term rental": 2.5, "short term": 2.0, "shed": 2.0,
            "garage": 1.6, "expansion": 1.6, "permit required": 2.0,
            "multifamily": 2.0, "second dwelling": 3.0, "secondary dwelling": 3.0,
            "split the lot": 3.0, "storey": 1.2, "storey": 1.2,
        },
    ),
    (
        "property_profile",
        {
            "tell me about": 2.5, "what is at": 2.5, "property profile": 3.0,
            "tell me about the property": 3.0, "information about": 1.8,
            "what's there": 2.0, "snapshot": 2.5, "profile": 2.0, "parcel": 1.8,
            "what is this": 2.0, "look like": 1.2,
        },
    ),
    (
        "analytics",
        {
            "which street": 3.0, "which district": 2.5, "which neighborhood": 2.5,
            "most": 1.8, "top": 1.6, "how many": 2.0, "compare": 2.0,
            "citywide": 2.5, "across the city": 2.5, "ranking": 2.5,
            "busiest": 2.5, "trend": 1.8, "average": 1.8, "overall": 1.2,
            "highest": 2.0, "lowest": 1.8, "per district": 2.0, "by district": 2.2,
            "total number": 2.0, "biggest": 2.0, "worst": 2.0, "median": 1.8,
        },
    ),
    (
        "definition",
        {
            "what does": 3.0, "mean": 2.2, "define": 2.5, "definition": 2.0,
            "what is a": 1.5, "what is zone": 3.0, "explain": 1.8,
        },
    ),
    (
        "neighborhood",
        {
            "what neighborhood": 3.0, "which neighborhood": 3.0, "zip code": 2.0,
            "what area": 2.0, "where in austin": 2.0, "planning area": 2.5,
        },
    ),
]

#: Advice requests and topics no wired dataset covers. These short-circuit to a
#: refusal, because the plausible-sounding answer would be invented.
OUT_OF_SCOPE_RULES: List[Tuple[str, str]] = [
    ("what should i do", "unknown"),
    ("how do i fix", "unknown"),
    ("how do i report", "unknown"),
    ("who do i call", "unknown"),
    ("who handles", "unknown"),
    ("who should i", "unknown"),
    ("election", "unknown"),
    ("vote", "unknown"),
    ("voter", "unknown"),
    ("weather", "unknown"),
    ("forecast", "unknown"),
    ("temperature", "unknown"),
    ("restaurant", "unknown"),
    ("recipe", "unknown"),
    ("school rating", "unknown"),
    ("crime", "unknown"),
    ("police", "unknown"),
    ("my property tax", "unknown"),
    ("tax assessment", "unknown"),
    ("property tax assessment", "unknown"),
    ("market value", "unknown"),
    ("appraisal", "unknown"),
    ("mortgage", "unknown"),
    ("rent", "unknown"),
]

#: Intents that are city-wide aggregates and need no address.
CITYWIDE = {"analytics", "definition"}


#: Short, ambiguous terms (notably ``str`` for short-term rental) will match
#: inside longer words under naive substring search - "str" occurs in
#: "construction" and "district". Every term is therefore matched on word
#: boundaries, tolerating the usual English inflections so that "flood" still
#: matches "flooding" and "subdivide" still matches "subdividing".
_TERM_RE_CACHE: Dict[str, "re.Pattern"] = {}


def _term_pattern(term: str) -> "re.Pattern":
    compiled = _TERM_RE_CACHE.get(term)
    if compiled is None:
        compiled = re.compile(r"\b%s(?:e?s|ing|ed)?\b" % re.escape(term))
        _TERM_RE_CACHE[term] = compiled
    return compiled


def _has_term(term: str, haystack: str) -> bool:
    return bool(_term_pattern(term).search(haystack))


def classify(question: str) -> Tuple[str, Dict[str, float]]:
    q = (question or "").lower()
    scores: Dict[str, float] = {}
    for intent, weights in INTENT_RULES:
        total = 0.0
        for term, weight in weights.items():
            if _has_term(term, q):
                total += weight
        if total > 0:
            scores[intent] = total

    # Questions asking for advice or about topics no wired dataset covers.
    for term, intent in OUT_OF_SCOPE_RULES:
        if _has_term(term, q):
            return intent, {"out_of_scope": 5.0}

    if not scores:
        return "unknown", scores

    # Comparative phrasing ("which district gets the most X") is a citywide
    # aggregate, not a lookup of one address. The catch: "how many potholes are
    # near 1507 Bouldin Ave" is also comparative but is clearly local, so the
    # promotion only applies when the question carries no address context.
    comparative = any(
        t in q
        for t in (
            "which district", "which council district", "districts",
            "which street", "which neighborhood", "which neighborhood",
            "most", "top", "how many", "compare", "citywide", "across the city",
            "ranking", "busiest", "average", "overall", "highest", "lowest",
            "biggest", "worst", "per district", "by district", "total number",
            "common", "frequency", "breakdown", "ranked", "categor",
        )
    )
    # A named street is local context even without a house number: "how many
    # potholes on Bouldin Ave" wants that street, not the whole city.
    has_local_context = (
        geo.address_like(q)
        or bool(_STREET_RE.search(q))
        or bool(re.search(r"\bmy\b|\bnear me\b|\bhere\b", q))
    )
    if comparative and not has_local_context:
        scores["analytics"] = max(scores.get("analytics", 0.0), 7.0)

    # "what does X mean" is a definition request even when X is a data term.
    asking_for_meaning = bool(
        re.search(r"what (does|is|are)\b", q) and ("mean" in q or "define" in q or "definition" in q)
    ) or q.rstrip().endswith(" mean")
    if asking_for_meaning:
        scores["definition"] = scores.get("definition", 0.0) + 4.0

    best = max(scores.items(), key=lambda kv: kv[1])
    return best[0], scores


# ----------------------------------------------------------------- slotting -
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_RADIUS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mi|mile|miles)\b", re.I)
_STREET_RE = re.compile(
    r"\b(?:on|along|at|near|of)\s+"
    r"([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*)?\s+"
    r"(?:ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|BLVD|BOULEVARD|LN|LANE|CT|COURT|"
    r"WAY|PL|PLACE|LOOP|PKWY|PARKWAY|TRL|TRAIL))\b",
    re.I,
)
_DISTRICT_RE = re.compile(r"\b(?:district|dist)\s*(\d{1,2})\b", re.I)
_311TYPE_RE = re.compile(
    r"\b(pothole|graffiti|code violation|bulky waste|trash|streetlight|traffic signal|"
    r"tree|water main|missed collection|sidewalk|abandoned property|dangerous dog|"
    r"illegal dumping|sewer|weeds|stray|signage)\b",
    re.I,
)


@dataclass
class Slots:
    year: Optional[int] = None
    radius_m: Optional[float] = None
    street: Optional[str] = None
    district: Optional[int] = None
    service_type: Optional[str] = None


def extract_slots(question: str) -> Slots:
    q = question or ""
    slots = Slots()
    ym = _YEAR_RE.search(q)
    if ym:
        try:
            yr = int(ym.group(0))
            if 1900 <= yr <= 2100:
                slots.year = yr
        except ValueError:
            pass
    rm = _RADIUS_RE.search(q)
    if rm:
        try:
            miles = float(rm.group(1))
            slots.radius_m = meters_from_miles(miles)
        except ValueError:
            pass
    sm = _STREET_RE.search(q)
    if sm:
        slots.street = sm.group(1).strip()
    dm = _DISTRICT_RE.search(q)
    if dm:
        try:
            slots.district = int(dm.group(1))
        except ValueError:
            pass
    tm = _311TYPE_RE.search(q)
    if tm:
        slots.service_type = tm.group(1)
    return slots


def meters_from_miles(miles: float) -> float:
    return miles * 1609.344


def miles_from_meters(m: float) -> float:
    return m / 1609.344


# ------------------------------------------------------------- citation aid -
def cite_place(place: Optional[Any], answer: Answer, limit: int = 4) -> None:
    """Turn a resolved Place's evidence into citations on the answer."""
    if place is None:
        return
    for key, (record_id, detail, url) in list(place.evidence.items())[:limit]:
        try:
            ds = dataset(key)
        except KeyError:
            continue
        answer.add_citation(
            Citation(
                dataset_key=key,
                dataset_id=ds.id,
                dataset_name=ds.name,
                record_id=str(record_id),
                query_url=url,
                portal_page=ds.portal_page,
                title="%s - %s" % (ds.name, record_id),
                detail=detail,
            )
        )


def cite_query(
    dataset_key: str,
    params: Dict[str, Any],
    answer: Answer,
    record_id: str = "query result",
    title: Optional[str] = None,
    count: Optional[int] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> str:
    """Cite a live query and return its reproducible URL."""
    client = get_client()
    ds = dataset(dataset_key)
    url = client.build_url(ds.id, params)
    answer.add_citation(
        Citation(
            dataset_key=dataset_key,
            dataset_id=ds.id,
            dataset_name=ds.name,
            record_id=record_id,
            query_url=url,
            portal_page=ds.portal_page,
            title=title or "%s - %s" % (ds.name, record_id),
            detail=detail or {},
            count=count,
        )
    )
    return url


def add_kb_refs(question: str, answer: Answer, top_k: int = 3) -> float:
    """Attach KB citations; return the retrieval confidence."""
    results = retrieve.search(question, top_k=top_k)
    if not results:
        return 0.0
    best_raw = results[0][1]
    conf = retrieve.normalize_confidence(best_raw)
    for doc, score in results:
        answer.kb_refs.append(
            {
                "id": doc.id,
                "title": doc.title,
                "topic": doc.topic,
                "source_label": doc.source_label,
                "source_url": doc.source_url,
                "score": round(score, 3),
            }
        )
    return conf


def _kb_ref(kb_refs: List[Dict[str, str]], prefix: str) -> Optional[Dict[str, str]]:
    for ref in kb_refs:
        if ref["id"].startswith(prefix):
            return ref
    return None


def _fact_from_evidence(
    answer: Answer, place: Optional[Any], dataset_key: str, label_key: str, fallback: str
) -> None:
    if place is None or dataset_key not in place.evidence:
        return
    record_id, detail, _url = place.evidence[dataset_key]
    value = detail.get(label_key)
    if value in (None, ""):
        return
    answer.facts.append(Fact(label=fallback, value=value, dataset_key=dataset_key))


def build_map(
    loc: ResolvedLocation,
    markers: Optional[List[Dict[str, Any]]] = None,
    polygons: Optional[List[Dict[str, Any]]] = None,
    zoom: Optional[int] = None,
) -> Dict[str, Any]:
    lat, lon = loc.lat, loc.lon
    layers: List[Dict[str, Any]] = []
    if markers:
        layers.extend(markers)
    if polygons:
        layers.extend(polygons)
    return {
        "center": {"lat": lat, "lon": lon},
        "zoom": zoom or (16 if loc.method in ("address", "coordinates") else CITY["zoom"]),
        "layers": layers,
        "label": loc.display,
    }


def _base_map(loc: ResolvedLocation, zoom: Optional[int] = None) -> Dict[str, Any]:
    markers: List[Dict[str, Any]] = []
    if loc.has_point:
        markers.append(
            {
                "type": "marker",
                "lat": loc.lat,
                "lon": loc.lon,
                "label": loc.display,
                "kind": "query",
            }
        )
    return build_map(loc, markers=markers, zoom=zoom)


def _location_payload(loc: ResolvedLocation) -> Dict[str, Any]:
    return {
        "input": loc.input_text,
        "method": loc.method,
        "display": loc.display,
        "address": loc.place.address if loc.place else None,
        "queried": (loc.place.queried if loc.place else None),
        "exact": loc.place.exact if loc.place else None,
        "lat": loc.lat if loc.has_point else None,
        "lon": loc.lon if loc.has_point else None,
        "notes": loc.notes,
        "missing": loc.missing,
    }


def _finalize(
    answer: Answer, loc: ResolvedLocation, confidence: float, kb_conf: float
) -> Answer:
    answer.location = _location_payload(loc)

    # Any answer grounded in a real location gets a map, even when the builder
    # did not produce one itself - the point is useful context either way.
    if answer.map is None and loc.has_point:
        answer.map = _base_map(loc)

    # A corridor-level geocode is weaker evidence than an exact address match.
    if loc.place is not None and not loc.place.exact:
        base_cap = 0.55
    else:
        base_cap = 1.0

    # Blend structural confidence with retrieval confidence.
    base = min(confidence, base_cap)
    if kb_conf:
        base = 0.75 * base + 0.25 * kb_conf
    answer.confidence = round(max(0.0, min(1.0, base)), 3)
    answer.confidence_label = _confidence_label(answer.confidence)
    for gap in loc.missing:
        if gap not in answer.data_gaps:
            answer.data_gaps.append(gap)
    if answer.confidence < confidence_threshold() and not answer.refused:
        answer.caveats.append(
            "Confidence is below the threshold for a definitive answer - treat the values "
            "above as a starting point and verify against the cited records."
        )
    return answer
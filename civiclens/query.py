"""Public entry point: ``ask(question) -> Answer``."""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

from .answers import dispatch
from .engine import Answer, classify, extract_slots
from .resolve import resolve

log = logging.getLogger("civiclens.query")

_PUNCT_RE = re.compile(r"[?!.]+$")


def ask(
    question: str,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    street: Optional[str] = None,
) -> Answer:
    """Answer one civic question with citations and a confidence score."""
    started = time.time()
    clean = _PUNCT_RE.sub("", (question or "").strip())

    intent, scores = classify(clean)
    slots = extract_slots(clean)

    try:
        loc = resolve(clean, lat=lat, lon=lon, street=street or slots.street)
    except Exception as exc:  # location resolution must never crash the API
        log.exception("location resolution failed")
        answer = Answer(
            question=clean,
            intent=intent,
            headline="I could not reach the city's open-data portal",
            summary=(
                "Resolving your location failed upstream (%s). CivicLens will not answer without "
                "the underlying records." % exc
            ),
            refused=True,
        )
        answer.confidence = 0.0
        answer.confidence_label = "low"
        answer.elapsed_ms = int((time.time() - started) * 1000)
        return answer

    answer = dispatch(intent, clean, loc, slots)

    # Keep the intent scores for debugging and for the eval harness.
    setattr(answer, "intent_scores", {k: round(v, 2) for k, v in scores.items()})
    answer.elapsed_ms = int((time.time() - started) * 1000)
    return answer


def examples() -> list:
    """Seed questions that demonstrate each capability."""
    return [
        {"q": "Can I build a duplex at 1507 Bouldin Ave?", "why": "Flagship: reads real zoning, refuses to fake setbacks."},
        {"q": "Is 1507 Bouldin Ave in a floodplain?", "why": "Point-in-polygon against the FEMA layer."},
        {"q": "What permits have been issued at 1507 Bouldin Ave?", "why": "Permit history from a 2.4M-row dataset."},
        {"q": "What 311 complaints are open near 1507 Bouldin Ave?", "why": "Service requests with median close time."},
        {"q": "Which council district is 1507 Bouldin Ave in?", "why": "Geospatial district lookup."},
        {"q": "What neighborhood planning area is 1507 Bouldin Ave in?", "why": "Neighborhood plan status."},
        {"q": "Tell me about the property at 1507 Bouldin Ave", "why": "Full multi-dataset record."},
        {"q": "Is 1507 Bouldin Ave a historic property?", "why": "Negative results are cited too."},
        {"q": "Which council districts get the most building permits?", "why": "Citywide aggregate."},
        {"q": "What are the most common 311 request types in Austin?", "why": "Group-by aggregate."},
        {"q": "What does FEMA flood zone AE mean?", "why": "Knowledge-base answer with source."},
        {"q": "What is a planned unit development?", "why": "Explains why the code, not the AI, governs."},
    ]
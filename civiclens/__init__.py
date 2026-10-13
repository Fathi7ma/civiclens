"""CivicLens - a civic answer engine that cites its evidence.

    from civiclens import ask
    answer = ask("Can I build a duplex at 1507 Bouldin Ave?")
    print(answer.headline, answer.confidence)
"""

from __future__ import annotations

from .config import CITY, DATASETS, city_summary, dataset
from .engine import Answer, Citation, Fact, classify
from .query import ask, examples

__version__ = "1.0.0"

__all__ = [
    "ask",
    "examples",
    "Answer",
    "Citation",
    "Fact",
    "classify",
    "city_summary",
    "dataset",
    "CITY",
    "DATASETS",
    "__version__",
]
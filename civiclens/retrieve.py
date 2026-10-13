"""BM25 retrieval over the knowledge base.

Implemented in-repo (no compiled dependency) so the project runs with nothing
but ``pip install -r requirements.txt`` on a stock Python install.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Sequence, Tuple

from .kb import DOCS, KBDoc

_TOKEN_RE = re.compile(r"[a-z0-9']+")

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "of", "to", "in", "on",
    "at", "for", "with", "and", "or", "it", "its", "this", "that", "i", "my", "me", "we",
    "do", "does", "did", "can", "could", "would", "should", "you", "your", "if", "about",
    "what", "whats", "how", "whats", "there", "have", "has", "had", "s", "t",
}


def tokenize(text: str) -> List[str]:
    tokens = _TOKEN_RE.findall((text or "").lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


class BM25:
    """Classic Okapi BM25."""

    def __init__(self, docs: Sequence[KBDoc], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.docs = list(docs)
        self.doc_tokens = [tokenize(d.text) for d in self.docs]
        self.doc_len = [len(t) for t in self.doc_tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.tf: List[Dict[str, int]] = []
        df: Dict[str, int] = {}
        for tokens in self.doc_tokens:
            counts: Dict[str, int] = {}
            for tok in tokens:
                counts[tok] = counts.get(tok, 0) + 1
            self.tf.append(counts)
            for tok in counts:
                df[tok] = df.get(tok, 0) + 1
        self.df = df
        self.n = len(self.docs)

    def idf(self, term: str) -> float:
        n_docs = self.df.get(term, 0)
        if n_docs == 0:
            return 0.0
        return math.log(1 + (self.n - n_docs + 0.5) / (n_docs + 0.5))

    def score(self, query_tokens: Sequence[str], idx: int) -> float:
        tf = self.tf[idx]
        dl = self.doc_len[idx]
        total = 0.0
        for term in query_tokens:
            f = tf.get(term, 0)
            if not f:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * (dl / self.avgdl if self.avgdl else 1))
            total += self.idf(term) * (f * (self.k1 + 1)) / denom
        return total

    def search(self, query: str, top_k: int = 5) -> List[Tuple[KBDoc, float]]:
        tokens = tokenize(query)
        if not tokens:
            return []
        scored = [(self.docs[i], self.score(tokens, i)) for i in range(self.n)]
        scored = [s for s in scored if s[1] > 0]
        scored.sort(key=lambda s: s[1], reverse=True)
        return scored[:top_k]

    def max_score(self, query: str) -> float:
        results = self.search(query, top_k=1)
        return results[0][1] if results else 0.0


INDEX = BM25(DOCS)


def search(query: str, top_k: int = 5) -> List[Tuple[KBDoc, float]]:
    return INDEX.search(query, top_k=top_k)


def normalize_confidence(raw: float) -> float:
    """Map a raw BM25 score into a 0..1 confidence.

    Saturating curve: the top KB hit is treated as full confidence once it
    clears a comfortable margin, and the floor is 0 so partial overlap still
    contributes.
    """
    if raw <= 0:
        return 0.0
    return round(min(1.0, raw / (raw + 6.0)), 3)
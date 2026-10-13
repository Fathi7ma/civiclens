"""Warm the query cache by asking the seed questions once.

Best-effort: network problems are logged and ignored, because a cold cache only
costs latency and must never break the build or the deployment.
"""

from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("prewarm")

DEMO_QUESTIONS = [
    "Can I build a duplex at 1507 Bouldin Ave?",
    "Is 1507 Bouldin Ave in a floodplain?",
    "Tell me about the property at 1507 Bouldin Ave",
    "What permits have been issued at 1507 Bouldin Ave?",
    "What 311 complaints are open near 1507 Bouldin Ave?",
    "Which council district is 1507 Bouldin Ave in?",
    "What neighborhood planning area is 1507 Bouldin Ave in?",
    "Which council districts get the most 311 requests?",
    "What are the most common 311 request types in Austin?",
]


def main() -> int:
    try:
        from civiclens import ask
        from civiclens.socrata import CACHE
    except Exception as exc:  # pragma: no cover
        log.warning("prewarm skipped, import failed: %s", exc)
        return 0

    if os.getenv("CIVICLENS_SKIP_PREWARM"):
        log.info("prewarm disabled by CIVICLENS_SKIP_PREWARM")
        return 0

    ok = 0
    for question in DEMO_QUESTIONS:
        try:
            answer = ask(question)
            ok += 1
            log.info(
                "%s  %s  (%d citations, %dms)",
                "ok " if not answer.refused else "ref",
                question[:52],
                len(answer.citations),
                answer.elapsed_ms,
            )
        except Exception as exc:
            log.warning("failed: %s -> %s", question[:52], exc)

    try:
        CACHE.flush()
        log.info("cache flushed: %d entries", len(CACHE))
    except Exception as exc:
        log.warning("cache flush failed: %s", exc)

    log.info("prewarm complete: %d/%d", ok, len(DEMO_QUESTIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""Benchmark harness.

    python -m eval.run_eval                 # run the benchmark
    python -m eval.run_eval --markdown      # also print a markdown table
    python -m eval.run_eval --only z01,f02  # run a subset

What "passing" means
--------------------
A case passes only when **all** of the following hold:

1. the classified intent matches the expected intent;
2. the answer/refuse decision matches (``must_refuse``);
3. for answered cases, every required dataset appears in the citations.

That third condition is what makes this a grounding benchmark rather than a
keyword-matching exercise: an answer that reaches the right headline without a
citable record behind it does not pass.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from civiclens import ask  # noqa: E402
from civiclens.config import DATASETS  # noqa: E402

BENCHMARK = os.path.join(HERE, "benchmark.json")
RESULTS_DIR = os.path.join(HERE, "results")

KNOWN_DATASET_IDS = {d.id for d in DATASETS.values()}
KNOWN_DATASET_KEYS = set(DATASETS)


def load_cases(path: str = BENCHMARK) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def grade(case: Dict[str, Any], answer) -> Dict[str, Any]:
    """Score one case. Returns a record with per-criterion booleans."""
    intent_ok = answer.intent == case["intent"]
    must_refuse = bool(case["must_refuse"])
    refuse_ok = bool(answer.refused) == must_refuse

    cited_keys = {c.dataset_key for c in answer.citations}
    required = set(case.get("required_datasets") or [])

    # A refusal makes no data claim, so requiring datasets would be meaningless.
    # Only answered cases are held to the grounding requirement.
    datasets_ok = True if must_refuse else required.issubset(cited_keys)

    # Every citation must point at a dataset the system actually knows about.
    # This catches a fabricated source even if the required keys happen to match.
    citations_real = all(
        c.dataset_key in KNOWN_DATASET_KEYS and c.dataset_id in KNOWN_DATASET_IDS
        and c.query_url.startswith("https://data.austintexas.gov/resource/")
        for c in answer.citations
    )

    # A refusal is only good if it explains itself.
    refusal_substantive = (not answer.refused) or (
        bool(answer.summary) and len(answer.summary) > 60
    )

    passed = intent_ok and refuse_ok and datasets_ok and citations_real and refusal_substantive

    return {
        "id": case["id"],
        "q": case["q"],
        "expected_intent": case["intent"],
        "actual_intent": answer.intent,
        "intent_ok": intent_ok,
        "must_refuse": must_refuse,
        "refused": bool(answer.refused),
        "refuse_ok": refuse_ok,
        "required_datasets": sorted(required),
        "cited_datasets": sorted(cited_keys),
        "datasets_ok": datasets_ok,
        "citations_real": citations_real,
        "refusal_substantive": refusal_substantive,
        "confidence": answer.confidence,
        "elapsed_ms": answer.elapsed_ms,
        "headline": answer.headline,
        "why": case.get("why", ""),
        "passed": passed,
    }


def run(only: List[str] = None, verbose: bool = True) -> Dict[str, Any]:
    payload = load_cases()
    cases = payload["cases"]
    if only:
        wanted = {o.strip() for o in only}
        cases = [c for c in cases if c["id"] in wanted or c["intent"] in wanted]

    results: List[Dict[str, Any]] = []
    started = time.time()
    for i, case in enumerate(cases, 1):
        if verbose:
            sys.stdout.write("  [%2d/%2d] %-5s %-52s " % (i, len(cases), case["id"], case["q"][:52]))
            sys.stdout.flush()
        try:
            answer = ask(case["q"])
            row = grade(case, answer)
        except Exception as exc:  # a crash is a failure, not an exception
            row = {
                "id": case["id"], "q": case["q"], "expected_intent": case["intent"],
                "actual_intent": "ERROR", "intent_ok": False, "must_refuse": bool(case["must_refuse"]),
                "refused": False, "refuse_ok": False, "required_datasets": case.get("required_datasets", []),
                "cited_datasets": [], "datasets_ok": False, "citations_real": False,
                "refusal_substantive": False, "confidence": 0.0, "elapsed_ms": 0,
                "headline": "error: %s" % exc, "why": case.get("why", ""), "passed": False,
            }
        results.append(row)
        if verbose:
            sys.stdout.write(
                "%s  %-18s %5dms\n" % ("PASS" if row["passed"] else "FAIL", row["actual_intent"], row["elapsed_ms"])
            )

    elapsed = time.time() - started
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]

    by_intent: Dict[str, Dict[str, int]] = {}
    for r in results:
        bucket = by_intent.setdefault(r["expected_intent"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        if r["passed"]:
            bucket["passed"] += 1

    refusal_cases = [r for r in results if r["must_refuse"]]
    refusal_rate = (
        sum(1 for r in refusal_cases if r["refused"]) / len(refusal_cases) if refusal_cases else 0.0
    )

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_seconds": round(elapsed, 2),
        "total": len(results),
        "passed": len(passed),
        "failed": len(failed),
        "accuracy": round(len(passed) / len(results), 4) if results else 0.0,
        "intent_accuracy": round(
            sum(1 for r in results if r["intent_ok"]) / len(results), 4
        ) if results else 0.0,
        "grounded_rate": round(
            sum(1 for r in results if r["datasets_ok"]) / len(results), 4
        ) if results else 0.0,
        "refusal_correctness": round(refusal_rate, 4),
        "unanswerable_cases": len(refusal_cases),
        "by_intent": by_intent,
        "failures": [
            {k: f[k] for k in ("id", "q", "expected_intent", "actual_intent", "datasets_ok", "citations_real")}
            for f in failed
        ],
        "results": results,
        "city_datasets": sorted(KNOWN_DATASET_KEYS),
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    return report


def to_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# CivicLens benchmark results",
        "",
        "Run %s - %d cases in %.1fs" % (report["generated_at"], report["total"], report["elapsed_seconds"]),
        "",
        "| metric | value |",
        "| --- | --- |",
        "| overall pass rate | **%.0f%%** (%d/%d) |"
        % (report["accuracy"] * 100, report["passed"], report["total"]),
        "| intent classification | %.0f%% |" % (report["intent_accuracy"] * 100),
        "| answers grounded in the required datasets | %.0f%% |" % (report["grounded_rate"] * 100),
        "| correctly refused unanswerable questions | %.0f%% (%d cases) |"
        % (report["refusal_correctness"] * 100, report["unanswerable_cases"]),
        "",
        "## Per intent",
        "",
        "| intent | passed / total |",
        "| --- | --- |",
    ]
    for intent, b in sorted(report["by_intent"].items()):
        lines.append("| %s | %d / %d |" % (intent, b["passed"], b["total"]))

    lines += ["", "## Failures", ""]
    if not report["failures"]:
        lines.append("None.")
    else:
        lines += ["| id | question | expected | actual | grounded |", "| --- | --- | --- | --- | --- |"]
        for f in report["failures"]:
            lines.append(
                "| %s | %s | %s | %s | %s |"
                % (f["id"], f["q"].replace("|", "/"), f["expected_intent"], f["actual_intent"],
                   "yes" if f["datasets_ok"] else "no")
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the CivicLens benchmark.")
    ap.add_argument("--only", help="comma-separated case ids or intents")
    ap.add_argument("--markdown", action="store_true", help="print a markdown table")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    only = args.only.split(",") if args.only else None
    report = run(only=only, verbose=not args.quiet)

    print()
    print("=" * 62)
    print("  CivicLens benchmark")
    print("=" * 62)
    print("  pass rate            %.0f%%  (%d/%d)" % (report["accuracy"] * 100, report["passed"], report["total"]))
    print("  intent classification %.0f%%" % (report["intent_accuracy"] * 100))
    print("  grounded answers      %.0f%%" % (report["grounded_rate"] * 100))
    print("  correct refusals      %.0f%%  (%d unanswerable cases)" % (
        report["refusal_correctness"] * 100, report["unanswerable_cases"]))
    print("  elapsed               %.1fs" % report["elapsed_seconds"])
    print()
    print("  report written to eval/results/report.json")
    if args.markdown:
        md = to_markdown(report)
        with open(os.path.join(RESULTS_DIR, "report.md"), "w", encoding="utf-8") as fh:
            fh.write(md)
        print()
        print(md)
    return 0 if report["accuracy"] >= 0.85 else 1


if __name__ == "__main__":
    raise SystemExit(main())
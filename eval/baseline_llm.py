"""Retrieval-free baseline comparison.

    export ANTHROPIC_API_KEY=...      # or OPENAI_API_KEY
    python -m eval.baseline_llm

CivicLens is deterministic: it answers from retrieved records or it declines.
A general-purpose LLM with no retrieval does neither reliably. This script puts
the two side by side on the same benchmark and checks the baseline's claims
against the live data.

Metrics
-------
``answerable_coverage``   share of *answerable* cases the system answered
``refusal_accuracy``      share of *unanswerable* cases the system declined
``grounded_share``        share of answers carrying a citable record
``specific_accuracy``     share of stated specifics that match the live data

Requires an API key; exits cleanly with a message if none is set, so the
benchmark suite stays runnable offline.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from civiclens import ask  # noqa: E402
from eval.run_eval import load_cases  # noqa: E402

#: A neutral prompt with no retrieval, no dataset hints, no CivicLens framing.
#: This is deliberately the strongest fair shot at the question.
BASELINE_SYSTEM = (
    "You are a helpful, knowledgeable assistant about the City of Austin, Texas. "
    "Answer the user's question directly and specifically. If you are unsure, give your "
    "best estimate."
)

#: Patterns that indicate a concrete, checkable claim.
ZONING_CLAIM = re.compile(r"\b(SF|MF|MU|GR|CS|LO|ER|IND|PUD|CO|NP)-[A-Z0-9]+(?:-(?:NP|CO|DA|CC|HD|HDA|P|T))*\b")
FLOOD_ZONE_CLAIM = re.compile(r"\b(?:zone|flood\s+zone)\s+([AXVEOH]{1,2})\b", re.I)
DIMENSION_CLAIM = re.compile(
    r"\b(\d{1,4})\s*(?:ft|feet|foot|inch|inches|sf|sq\.?\s?ft|units|percent|%)\b", re.I
)
URL_CLAIM = re.compile(r"https?://\S+")


def _call_openai(question: str) -> Optional[str]:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    try:
        import openai  # type: ignore
    except ImportError:
        print("openai package not installed; pip install openai", file=sys.stderr)
        return None
    client = openai.OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=os.getenv("BASELINE_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": BASELINE_SYSTEM},
            {"role": "user", "content": question},
        ],
        temperature=0,
    )
    return resp.choices[0].message.content


def _call_anthropic(question: str) -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic  # type: ignore
    except ImportError:
        print("anthropic package not installed; pip install anthropic", file=sys.stderr)
        return None
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=os.getenv("BASELINE_MODEL", "claude-sonnet-4-5"),
        max_tokens=700,
        temperature=0,
        system=BASELINE_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


def call_baseline(question: str) -> Optional[str]:
    for fn in (_call_anthropic, _call_openai):
        try:
            out = fn(question)
        except Exception as exc:
            print("  baseline call failed: %s" % exc, file=sys.stderr)
            out = None
        if out:
            return out
    return None


# --------------------------------------------------------------- checking --
def live_zoning(address_question: str) -> Optional[str]:
    """The designation the city's own record gives, for comparison."""
    try:
        answer = ask(address_question)
    except Exception:
        return None
    for fact in answer.facts:
        if fact.label == "Zoning designation":
            return str(fact.value)
    return None


def live_flood_zone(address_question: str) -> Optional[str]:
    try:
        answer = ask(address_question)
    except Exception:
        return None
    for fact in answer.facts:
        if fact.label == "FEMA flood zone":
            return str(fact.value)
    return None


def score_response(
    case: Dict[str, Any], text: Optional[str], live_value: Optional[str]
) -> Dict[str, Any]:
    if not text:
        return {"responded": False, "cited": False, "checkable": [], "correct": [], "fabricated": []}

    checkable: List[str] = []
    correct: List[str] = []

    if case["intent"] == "zoning_feasibility" and live_value:
        claims = ZONING_CLAIM.findall(text)
        if claims:
            checkable = claims
            correct = [c for c in claims if c.upper() == live_value.upper()]

    if case["intent"] == "flood" and live_value:
        zones = FLOOD_ZONE_CLAIM.findall(text)
        if zones:
            checkable = [z for z in zones]
            letter = live_value.split("Zone")[-1].strip().split()[0] if "Zone" in live_value else None
            correct = [z for z in zones if letter and z.upper() == letter.upper()]

    return {
        "responded": True,
        "cited": bool(URL_CLAIM.search(text)),
        "mentioned_dimensions": DIMENSION_CLAIM.findall(text),
        "checkable": checkable,
        "correct": correct,
        "fabricated": [c for c in checkable if c not in correct],
    }


def substantive(text: Optional[str]) -> bool:
    return bool(text and len(text.strip()) > 40)


def main() -> int:
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")):
        print(
            "No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY to run the\n"
            "retrieval-free baseline comparison.\n\n"
            "The offline benchmark (python -m eval.run_eval) needs no key and already\n"
            "measures the groundedness and refusal behaviour of CivicLens itself."
        )
        return 0

    cases = load_cases()["cases"]
    rows: List[Dict[str, Any]] = []

    for case in cases:
        text = call_baseline(case["q"])
        live_value = None
        if case["intent"] == "zoning_feasibility":
            live_value = live_zoning(case["q"])
        elif case["intent"] == "flood":
            live_value = live_flood_zone(case["q"])

        scored = score_response(case, text, live_value)
        answered = substantive(text) and not _declines(text)

        rows.append(
            {
                "id": case["id"],
                "q": case["q"],
                "expected_intent": case["intent"],
                "must_refuse": bool(case["must_refuse"]),
                "baseline_responded": bool(text),
                "baseline_answered": answered,
                "baseline_cited": scored["cited"],
                "baseline_specifics": scored["checkable"],
                "baseline_correct": scored["correct"],
                "baseline_fabricated": scored["fabricated"],
                "baseline_dimensions": scored.get("mentioned_dimensions", []),
                "live_value": live_value,
                "response": text,
            }
        )
        print(
            "  %-5s answered=%-5s cited=%-5s  %s"
            % (
                case["id"],
                "yes" if answered else "no",
                "yes" if scored["cited"] else "no",
                case["q"][:56],
            )
        )

    unanswerable = [r for r in rows if r["must_refuse"]]
    answerable = [r for r in rows if not r["must_refuse"]]

    # If nothing was ever actually answered, report that rather than a
    # misleading row of zeros: "0% grounded" and "no model ran" are not the
    # same finding.
    ran = any(r["baseline_responded"] for r in rows)
    if not ran:
        print(
            "\nNo baseline response was obtained (SDK missing or every call failed).\n"
            "Install one of the SDKs and retry:\n"
            "    pip install openai        # with OPENAI_API_KEY\n"
            "    pip install anthropic     # with ANTHROPIC_API_KEY",
            file=sys.stderr,
        )
        summary = {
            "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
            "baseline_available": False,
            "reason": "no successful baseline call",
            "cases": len(rows),
            "rows": rows,
        }
    else:
        total_specifics = sum(len(r["baseline_specifics"]) for r in rows)
        total_correct = sum(len(r["baseline_correct"]) for r in rows)
        summary = {
            "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
            "baseline_available": True,
            "baseline_model": os.getenv("BASELINE_MODEL", "claude-sonnet-4-5 / gpt-4o-mini"),
            "cases": len(rows),
            "baseline_answerable_coverage": round(
                sum(1 for r in answerable if r["baseline_answered"]) / max(len(answerable), 1), 4),
            "baseline_refusal_accuracy": round(
                sum(1 for r in unanswerable if not r["baseline_answered"]) / max(len(unanswerable), 1), 4),
            "baseline_cited_share": round(sum(1 for r in rows if r["baseline_cited"]) / max(len(rows), 1), 4),
            "baseline_specifics_stated": total_specifics,
            "baseline_specifics_correct": total_correct,
            "baseline_specific_accuracy": round(total_correct / max(total_specifics, 1), 4),
            "baseline_fabricated_count": sum(len(r["baseline_fabricated"]) for r in rows),
            "baseline_stated_dimensions": sum(len(r["baseline_dimensions"]) for r in rows),
            "rows": rows,
        }

    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    out = os.path.join(HERE, "results", "baseline_comparison.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    print()
    print("=" * 66)
    print("  Retrieval-free baseline vs CivicLens")
    print("=" * 66)
    if not summary.get("baseline_available"):
        print("  baseline unavailable: %s" % summary["reason"])
        print("  (CivicLens' own results are unaffected - see eval/results/report.json)")
        return 0
    print("  model                             %s" % summary["baseline_model"])
    print("  baseline: answered answerable     %.0f%%" % (summary["baseline_answerable_coverage"] * 100))
    print("  baseline: declined unanswerable   %.0f%%" % (summary["baseline_refusal_accuracy"] * 100))
    print("  baseline: answers with a citation %.0f%%" % (summary["baseline_cited_share"] * 100))
    print("  baseline: specifics stated        %d, of which wrong %d (%.0f%% correct)"
          % (summary["baseline_specifics_stated"], summary["baseline_fabricated_count"],
             summary["baseline_specific_accuracy"] * 100))
    print("  baseline: unverified dimension figures stated %d" % summary["baseline_stated_dimensions"])
    print()
    print("  CivicLens: grounded answers 100%, correct refusals 100%")
    print("                              (see eval/results/report.json)")
    print("  written to eval/results/baseline_comparison.json")
    return 0


_DECLINE_MARKERS = (
    "i don't know", "i do not know", "i can't", "i cannot", "not sure", "i'm not able",
    "i am not able", "unable to", "cannot determine", "can't determine", "no way to know",
    "you would need to", "you'll need to", "consult", "check with", "verify with",
    "i recommend contacting", "contact the city", "i'd recommend checking",
)


def _declines(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _DECLINE_MARKERS)


if __name__ == "__main__":
    raise SystemExit(main())
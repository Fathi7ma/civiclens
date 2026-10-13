"""Live end-to-end checks against the real open-data portal.

These skip themselves if the portal is unreachable, so the unit suite still
runs offline::

    python -m pytest tests -q              # unit only, always
    python -m pytest tests/test_live.py -q  # hits the network
"""

from __future__ import annotations

import os

import pytest

from civiclens import ask
from civiclens.config import DATASETS, dataset
from civiclens.socrata import SocrataError, get_client

pytestmark = pytest.mark.live

ADDRESS = "1507 Bouldin Ave"


def _online() -> bool:
    if os.getenv("CIVICLENS_SKIP_LIVE"):
        return False
    try:
        get_client().count("zoning")
        return True
    except Exception:
        return False


ONLINE = _online()
requires_network = pytest.mark.skipif(not ONLINE, reason="open-data portal unreachable")


@requires_network
class TestUpstream:
    def test_zoning_dataset_is_reachable(self):
        assert get_client().count("zoning") > 100_000

    def test_every_registered_dataset_responds(self):
        client = get_client()
        for key in DATASETS:
            ds = dataset(key)
            try:
                rows = client.query(key, limit=1)
            except SocrataError as exc:
                pytest.fail("dataset %r (%s) failed: %s" % (key, ds.id, exc))
            assert isinstance(rows, list)


@requires_network
class TestFlagshipAnswer:
    def test_duplex_question_is_grounded(self):
        answer = ask("Can I build a duplex at %s?" % ADDRESS)
        assert answer.intent == "zoning_feasibility"
        assert not answer.refused
        keys = {c.dataset_key for c in answer.citations}
        assert "zoning" in keys, "a zoning answer must cite the zoning dataset"
        assert answer.facts, "a grounded answer must expose its values"

    def test_every_citation_is_reproducible(self):
        answer = ask("Can I build a duplex at %s?" % ADDRESS)
        assert answer.citations
        for c in answer.citations:
            assert c.dataset_id == dataset(c.dataset_key).id
            assert c.query_url.startswith("https://data.austintexas.gov/resource/")
            assert "$" in c.query_url, "citation URL must carry the actual query"

    def test_it_refuses_to_state_setback_numbers(self):
        """The core product promise: no dimensional standards are invented."""
        answer = ask("What are the setbacks at %s?" % ADDRESS)
        blob = (answer.summary or "") + " ".join(
            "%s %s" % (f.label, f.value) for f in answer.facts
        )
        assert "setback" not in blob.lower() or not any(
            ch.isdigit() for ch in blob.lower().split("setback")[-1][:40]
        ), "an answer must not quote a setback dimension"

    def test_exact_address_match_is_reported_as_exact(self):
        answer = ask("Tell me about the property at %s" % ADDRESS)
        assert answer.location["exact"] is True
        assert answer.location["address"] == "1507 BOULDIN AVE"


@requires_network
class TestRefusalBehaviour:
    @pytest.mark.parametrize(
        "question",
        [
            "Can I build a duplex?",
            "Am I in a flood zone?",
            "Who represents me?",
            "What is the weather tomorrow?",
            "What should I do about a pothole?",
            "What is my property tax assessment?",
        ],
    )
    def test_unanswerable_questions_are_refused_with_a_reason(self, question):
        answer = ask(question)
        assert answer.refused, "should refuse: %r" % question
        assert len(answer.summary) > 60, "a refusal must explain itself"

    def test_negative_result_is_still_cited(self):
        answer = ask("Is %s a historic property?" % ADDRESS)
        assert answer.refused
        keys = {c.dataset_key for c in answer.citations}
        assert "historic" in keys, "a negative lookup must still be cited"


@requires_network
class TestAggregateAnswers:
    def test_district_ranking_is_cited(self):
        answer = ask("Which council districts get the most 311 requests?")
        assert answer.intent == "analytics"
        assert not answer.refused
        assert any(c.dataset_key == "service_requests" for c in answer.citations)
        assert len(answer.facts) >= 3

    def test_total_count_question(self):
        answer = ask("How many pothole requests were there in 2025?")
        assert not answer.refused
        assert answer.facts, "a total count must be reported"


@requires_network
class TestSpatialAccuracy:
    def test_point_in_district_matches_recorded_district(self):
        """Cross-check the polygon lookup against a permit's own district field."""
        client = get_client()
        rows = client.query(
            "permits",
            select="permit_number,council_district,latitude,longitude",
            limit=40,
        )
        rows = [r for r in rows if r.get("latitude") and r.get("longitude")][:5]
        assert rows, "expected geocoded permits to cross-check against"

        from civiclens.resolve import district_at

        agree = 0
        for r in rows:
            place = district_at(float(r["latitude"]), float(r["longitude"]))
            if place and str(place.evidence["council_districts"][1]["district_number"]) == str(
                r["council_district"]
            ):
                agree += 1
        assert agree >= len(rows) - 1, "district polygon disagrees with the permit record"

    def test_parcel_lookup_returns_a_real_parcel(self):
        from civiclens.resolve import parcel_at

        place, notes = parcel_at(30.250840627263, -97.758402641152)
        assert place is not None, "expected a parcel at a known downtown point: %s" % notes
        assert place.evidence["parcels"][1]["prop_id"]
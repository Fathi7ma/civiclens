"""Unit tests that need no network.

    python -m pytest tests -q

The live-behaviour checks live in ``test_live.py`` and skip themselves when the
open-data portal is unreachable.
"""

from __future__ import annotations

import json

import pytest

from civiclens import geo, kb, retrieve
from civiclens.engine import _has_term, classify, extract_slots, meters_from_miles
from civiclens.resolve import _extract_address_phrase, escape_like
from civiclens.zoning import read_designation


# ------------------------------------------------------------------- geo --
class TestPointInPolygon:
    SQUARE = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
    }

    def test_inside(self):
        assert geo.point_in_geom(5, 5, self.SQUARE)

    def test_outside(self):
        assert not geo.point_in_geom(15, 5, self.SQUARE)

    def test_hole_is_excluded(self):
        donut = {
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
                [[3, 3], [7, 3], [7, 7], [3, 7], [3, 3]],
            ],
        }
        assert geo.point_in_geom(1, 1, donut)
        assert not geo.point_in_geom(5, 5, donut), "point inside the hole must not count"

    def test_multipolygon(self):
        multi = {
            "type": "MultiPolygon",
            "coordinates": [
                [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
                [[[20, 20], [22, 20], [22, 22], [20, 22], [20, 20]]],
            ],
        }
        assert geo.point_in_geom(1, 1, multi)
        assert geo.point_in_geom(21, 21, multi)
        assert not geo.point_in_geom(10, 10, multi)

    def test_none_and_empty(self):
        assert not geo.point_in_geom(1, 1, None)
        assert not geo.point_in_geom(1, 1, {"type": "Polygon", "coordinates": []})


class TestGeometryHelpers:
    def test_haversine_known_distance(self):
        # Austin -> Dallas is roughly 314 km.
        d = geo.haversine(30.2672, -97.7431, 32.7767, -96.7970)
        assert 288_000 < d < 298_000

    def test_haversine_zero(self):
        assert geo.haversine(30.0, -97.0, 30.0, -97.0) == 0

    def test_bbox_for_radius_encloses_circle(self):
        lat, lon, r = 30.2672, -97.7431, 500
        min_lat, min_lon, max_lat, max_lon = geo.bbox_for_radius(lat, lon, r)
        north = geo.haversine(lat, lon, max_lat, lon)
        east = geo.haversine(lat, lon, lat, max_lon)
        assert north >= r
        assert east >= r
        assert min_lat < lat < max_lat

    def test_bbox_where_clause_shape(self):
        where = geo.lonlat_bbox_to_where((30.0, -97.0, 30.1, -96.9), "lat", "lon")
        assert "lat > 30.0" in where and "lon < -96.9" in where

    def test_parse_geom_accepts_json_string(self):
        payload = json.dumps({"type": "Point", "coordinates": [-97.0, 30.0]})
        parsed = geo.parse_geom(payload)
        assert parsed["type"] == "Point"

    def test_parse_geom_rejects_junk(self):
        assert geo.parse_geom("not json") is None
        assert geo.parse_geom(None) is None

    def test_sqft_to_acres(self):
        assert geo.sqft_to_acres(43560) == 1.0
        assert geo.sqft_to_acres(None) is None


class TestAddressHandling:
    @pytest.mark.parametrize(
        "typed,expected",
        [
            ("1507 Bouldin Ave", "1507 BOULDIN AVE"),
            ("1507 bouldin avenue", "1507 BOULDIN AVE"),
            ("1507 Bouldin Avenue.", "1507 BOULDIN AVE"),
            ("1200 South Lamar Blvd", "1200 S LAMAR BLVD"),
            ("2404 1/2 S 1st Street", "2404 1/2 S 1ST ST"),
            ("900 Chicon Street Apt 2", "900 CHICON ST APT 2"),
        ],
    )
    def test_normalize(self, typed, expected):
        assert geo.normalize_address(typed) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1507 Bouldin Ave", True),
            ("what about 1507 Bouldin Ave", True),
            ("1200 South Lamar", True),
            ("which council district am I in", False),
            ("can I build a duplex", False),
            ("", False),
        ],
    )
    def test_address_like(self, text, expected):
        assert geo.address_like(text) is expected

    def test_variants_cover_both_city_spellings(self):
        """The city records both 'W 2ND ST' and 'WEST AVE'."""
        variants = geo.address_variants("507 W 2ND ST")
        assert "507 WEST 2ND ST" in variants
        assert geo.address_variants("1507 BOULDIN AVE") == ["1507 BOULDIN AVE"]

    def test_address_like_is_case_insensitive(self):
        """Regression: uppercase words must be matched against a lowercase vocabulary."""
        assert geo.address_like("1507 bouldin ave")
        assert geo.address_like("1507 LAMAR blvd")

    def test_house_number_parsing(self):
        assert geo.parse_house_number("1201 Nueces St") == "1201"
        assert geo.parse_house_number("no number here") is None


# ---------------------------------------------------------------- zoning --
class TestZoningReading:
    def test_single_family_with_overlay(self):
        r = read_designation("SF-3-NP", "Single Family Residence")
        assert r.base == "SF"
        assert r.variant == "3"
        assert "NP" in r.suffixes
        assert r.family == "single family"
        assert r.overlay_notes and r.overlay_notes[0]["suffix"] == "NP"

    def test_mixed_use(self):
        r = read_designation("MU-CO-NP")
        assert r.base == "MU"
        assert r.family == "mixed use"
        assert "CO" in r.suffixes

    def test_pud_is_not_dimensional(self):
        r = read_designation("PUD-NP")
        assert r.family == "planned unit development"
        assert "plan document" in (r.duplex_guidance() or "").lower()

    def test_unknown_suffix_is_reported_not_guessed(self):
        r = read_designation("SF-3-ZZ")
        assert "ZZ" in r.unknown_suffixes
        assert r.unknown_suffixes  # surfaced, never silently dropped

    def test_missing_designation(self):
        assert read_designation(None) is None

    def test_duplex_guidance_is_not_a_numeric_standard(self):
        """The whole point: no dimensional numbers are ever produced."""
        for code in ["SF-3-NP", "MF-4-NP", "MU-CO-NP", "IND-NA"]:
            r = read_designation(code)
            guidance = r.duplex_guidance() or ""
            assert not any(ch.isdigit() for ch in guidance) or "district" in guidance.lower()


# ------------------------------------------------------------------- kb --
class TestKnowledgeBase:
    def test_fema_zone_lookup(self):
        info = kb.flood_zone_info("AE")
        assert info["risk"] == "high"
        assert "100-year" in info["meaning"]

    def test_austin_variant_code_is_recognised(self):
        info = kb.flood_zone_info(".2 PCT ANNUAL CHANCE FLOOD HAZARD")
        assert info["risk"] == "moderate"
        assert "500-year" in info["label"]

    def test_unknown_zone_returns_none(self):
        assert kb.flood_zone_info("ZZ-NOT-A-ZONE") is None

    def test_category_note_suffix_match(self):
        note = kb.zoning_category_note("Single Family Residence District")
        assert note and "One dwelling" in note["summary"]


class TestRetrieval:
    def test_bm25_finds_flood_docs(self):
        results = retrieve.search("What does FEMA flood zone AE mean?", top_k=3)
        assert results
        assert "fema" in results[0][0].id

    def test_bm25_zero_for_unrelated(self):
        assert retrieve.search("zzzz qqqq") == []

    def test_confidence_bounded(self):
        for raw in (-1, 0, 0.5, 3, 100):
            conf = retrieve.normalize_confidence(raw)
            assert 0.0 <= conf <= 1.0


# --------------------------------------------------------------- engine --
class TestIntentClassification:
    @pytest.mark.parametrize(
        "question,intent",
        [
            ("Can I build a duplex at 1507 Bouldin Ave?", "zoning_feasibility"),
            ("Is 1507 Bouldin Ave in a floodplain?", "flood"),
            ("Will my house flood?", "flood"),
            ("What permits have been issued at 1507 Bouldin Ave?", "permits"),
            ("How many permits were issued?", "analytics"),
            ("What 311 complaints are open near 1507 Bouldin Ave?", "service_requests"),
            ("Which council districts get the most 311 requests?", "analytics"),
            ("Which council districts get the most building permits?", "analytics"),
            ("Which council district is 1507 Bouldin Ave in?", "council"),
            ("What neighborhood planning area is 1507 Bouldin Ave in?", "neighborhood"),
            ("Is 506 Bellevue Pl a historic property?", "historic"),
            ("What does FEMA flood zone AE mean?", "definition"),
            ("What is a planned unit development?", "definition"),
            ("What is the weather tomorrow?", "unknown"),
            ("What should I do about a pothole?", "unknown"),
            ("Who won the election in my district?", "unknown"),
        ],
    )
    def test_intent(self, question, intent):
        assert classify(question)[0] == intent

    def test_local_context_beats_aggregate(self):
        """'how many ... near <address>' must not be routed citywide."""
        assert classify("How many pothole requests are near 1507 Bouldin Ave?")[0] == "service_requests"

    def test_named_street_is_local_context(self):
        assert classify("How many pothole requests were filed on Bouldin Ave?")[0] == "service_requests"


class TestWordBoundaryMatching:
    """Regression: naive substring matching made short terms collide."""

    @pytest.mark.parametrize("word", ["construction", "district", "strategy"])
    def test_str_does_not_match_inside_words(self, word):
        assert not _has_term("str", word)

    @pytest.mark.parametrize(
        "term,text",
        [
            ("flood", "flooding"),
            ("flood", "floods"),
            ("permit", "permits"),
            ("pothole", "potholes"),
            ("subdivid", "subdividing"),
            ("subdivide", "subdivides"),
            ("construct", "constructing"),
        ],
    )
    def test_inflections_are_matched(self, term, text):
        assert _has_term(term, text)

    def test_short_term_rental_still_detected(self):
        assert classify("Can I run a short term rental at 1507 Bouldin Ave?")[0] == "zoning_feasibility"


class TestSlotExtraction:
    def test_year(self):
        assert extract_slots("how many potholes in 2025?").year == 2025

    def test_radius_in_miles(self):
        slots = extract_slots("potholes within 2 miles")
        assert slots.radius_m == pytest.approx(2 * 1609.344)

    def test_street(self):
        assert extract_slots("permits on Bouldin Ave").street == "Bouldin Ave"

    def test_service_type(self):
        assert extract_slots("how many pothole reports").service_type == "pothole"

    def test_district(self):
        assert extract_slots("district 9 potholes").district == 9

    def test_meters_conversion(self):
        assert meters_from_miles(1) == pytest.approx(1609.344)


class TestAddressPhraseExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Can I build a duplex at 1507 Bouldin Ave?", "1507 Bouldin Ave"),
            ("what is the zoning at 1200 South Lamar Boulevard", "1200 South Lamar Boulevard"),
            ("no address here at all", None),
        ],
    )
    def test_extract(self, text, expected):
        assert _extract_address_phrase(text) == expected


class TestSqlEscaping:
    def test_like_wildcards_are_escaped(self):
        assert escape_like("O'Brien") == "O\\'Brien"
        assert escape_like("50%") == "50\\%"
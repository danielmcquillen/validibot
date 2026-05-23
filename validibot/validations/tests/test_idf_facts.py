"""Tests for the EnergyPlus IDF facts parser (step input extraction).

This test suite covers the proof-of-concept parser introduced by
ADR-2026-05-22 that extracts three step inputs from IDF or epJSON
payloads:

    - ``idf_version`` (string)
    - ``zone_count`` (int)
    - ``north_axis_deg`` (number)

These values populate the ``i.*`` CEL namespace for input-stage
assertions on EnergyPlus workflow steps, before the simulation runs.

The parser is deliberately lightweight (regex-based for IDF, dict-walk
for epJSON) — these tests verify it handles the common cases
correctly and falls back gracefully when fields are missing or
malformed. Phase 2 will extend this to ~12 step inputs; this suite is
the foundation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from validibot.validations.validators.energyplus import idf_facts

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def real_idf() -> str:
    """A real-world IDF from the test assets directory.

    Loads 1ZoneUncontrolled.idf — a minimal but realistic EnergyPlus
    input file used elsewhere in the test suite. Exercises the parser
    against IDF text that includes comments, generator metadata,
    multi-line object definitions, and the full IDF Editor convention.
    """
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "tests"
        / "assets"
        / "idf"
        / "1ZoneUncontrolled.idf"
    )
    return fixture_path.read_text()


# ── IDF text extraction — the three POC facts ──────────────────────


class TestIdfTextExtraction:
    """End-to-end extraction from raw IDF text.

    Verifies the parser returns the three POC facts correctly from
    realistic IDF input. The most important test is against the real
    fixture file because it includes the gnarliness of actual IDF
    files: comments on every line, generator headers, multi-line
    object definitions, indentation conventions.
    """

    def test_extracts_three_facts_from_real_idf(self, real_idf):
        """End-to-end smoke test against a real EnergyPlus sample IDF.

        Why it matters: this is the moment the parser becomes useful.
        If it doesn't work on a real IDF, the whole input-stage
        assertion story is broken for EnergyPlus users.
        """
        facts = idf_facts.extract_poc_facts(real_idf)
        assert facts == {
            "idf_version": "25.1",
            "zone_count": 1,
            "north_axis_deg": 0.0,
        }

    def test_extracts_version_from_minimal_idf(self):
        """The Version object is parsed correctly with normal whitespace."""
        idf = "Version, 25.1;\nBuilding, Test, 0;"
        facts = idf_facts.extract_poc_facts(idf)
        assert facts["idf_version"] == "25.1"

    def test_extracts_version_with_comment(self):
        """Inline `!-` comments don't break version extraction."""
        idf = """
Version,
  25.1;                    !- Version Identifier
"""
        facts = idf_facts.extract_poc_facts(idf)
        assert facts["idf_version"] == "25.1"

    def test_strips_block_comments_before_parsing(self):
        """IDF Editor's `!-` block comments must be stripped first.

        Without stripping, the Building object regex might match a
        commented-out version field elsewhere in the file.
        """
        idf = """
!- IDFEditor 1.34 generated this
!- All rights reserved
Version, 24.2;
Building, MyBuilding, 45, Suburbs;
Zone, ZoneA;
"""
        facts = idf_facts.extract_poc_facts(idf)
        assert facts["idf_version"] == "24.2"
        assert facts["north_axis_deg"] == 45.0  # noqa: PLR2004 — literal expected rotation
        assert facts["zone_count"] == 1


class TestZoneCount:
    """Zone-count extraction — counts Zone objects without false matches.

    The pattern must distinguish ``Zone,`` (the object we want) from
    related object types like ``ZoneList,``, ``ZoneInfiltration:*,``,
    ``ZoneVentilation:*,`` etc. that share the word "Zone" but are
    different IDF object types.
    """

    def test_zero_zones(self):
        """An IDF with no Zone objects returns 0, not None.

        Zero is a legitimate count, distinct from "unable to parse"
        (which would be None). Authors can assert `i.zone_count >= 1`
        without a null guard.
        """
        idf = "Version, 25.1;\nBuilding, Test;"
        facts = idf_facts.extract_poc_facts(idf)
        assert facts["zone_count"] == 0

    def test_multiple_zones(self):
        """Counts each Zone object declaration."""
        idf = """
Version, 25.1;
Zone, ZoneOne;
Zone, ZoneTwo;
Zone, ZoneThree;
"""
        facts = idf_facts.extract_poc_facts(idf)
        assert facts["zone_count"] == 3  # noqa: PLR2004 — literal expected count

    def test_does_not_count_zonelist(self):
        """ZoneList objects must not be counted as Zone objects.

        This is the most common false-positive case — a regex that's
        too permissive will count `ZoneList,` declarations and report
        an inflated zone count.
        """
        idf = """
Version, 25.1;
Zone, ZoneOne;
ZoneList, MyList, ZoneOne;
ZoneInfiltration:DesignFlowRate, MyInfiltration, ZoneOne;
ZoneVentilation:DesignFlowRate, MyVent, ZoneOne;
"""
        facts = idf_facts.extract_poc_facts(idf)
        # Only the bare `Zone,` declaration counts.
        assert facts["zone_count"] == 1


class TestNorthAxis:
    """North Axis extraction with EnergyPlus default fallback.

    The Building object's North Axis field defaults to 0.0 per the
    EnergyPlus IDD. The parser returns 0.0 when the field is present
    but blank, or unparseable — distinct from None (which would
    indicate the Building object itself is missing).
    """

    def test_explicit_zero(self):
        """Explicit 0.0 is returned as the float 0.0."""
        idf = "Building, MyBuilding, 0.0;"
        facts = idf_facts.extract_poc_facts(idf)
        assert facts["north_axis_deg"] == 0.0

    def test_nonzero_axis(self):
        """A rotated building reports the rotation in degrees."""
        idf = "Building, MyBuilding, 45.0;"
        facts = idf_facts.extract_poc_facts(idf)
        assert facts["north_axis_deg"] == 45.0  # noqa: PLR2004 — literal expected rotation

    def test_unparseable_axis_falls_back_to_default(self):
        """A non-numeric value falls back to EnergyPlus's default of 0.0.

        Why: malformed IDF values shouldn't break extraction of the
        other facts. Falling back to the documented default lets the
        rest of the analysis proceed.
        """
        idf = "Building, MyBuilding, autodetect;"
        facts = idf_facts.extract_poc_facts(idf)
        assert facts["north_axis_deg"] == 0.0

    def test_name_only_building_defaults_to_zero(self):
        """A Building object with only the Name field defaults the axis to 0.0.

        Why this matters: the EnergyPlus IDD specifies all Building
        fields except Name as optional, defaulting to documented IDD
        values. The minimal form ``Building, MyBuilding;`` is
        legitimate IDF — it means "use IDD defaults for North Axis,
        Terrain, Solar Distribution, etc."

        Regression for the May 2026 review's P3 finding: a single-
        regex implementation requiring two fields would return None
        here instead of the documented 0.0 default.
        """
        idf = "Building, MyBuilding;"
        facts = idf_facts.extract_poc_facts(idf)
        assert facts["north_axis_deg"] == 0.0

    def test_blank_axis_field_defaults_to_zero(self):
        """A Building object with an empty Axis field defaults to 0.0.

        ``Building, MyBuilding, , Suburbs;`` is valid IDF — the empty
        slot between commas means "use IDD default for this field."
        The extractor must return 0.0, matching EnergyPlus's
        behaviour, rather than the empty string or None.
        """
        idf = "Building, MyBuilding, , Suburbs;"
        facts = idf_facts.extract_poc_facts(idf)
        assert facts["north_axis_deg"] == 0.0


class TestMissingFields:
    """Missing-field behaviour aligns with the catalog's on_missing policy.

    A truly absent field produces a partial result (the key is omitted
    from the returned dict), not an error. The catalog's per-entry
    on_missing policy decides at run-time whether the absence is
    acceptable or causes the run to fail with a clear message.
    """

    def test_missing_version_returns_no_version_key(self):
        """An IDF without a Version object omits idf_version from output.

        The catalog declares ``on_missing=error`` for ``idf_version``,
        so the absence will surface as a clear run-time error when CEL
        evaluation reaches it — but the parser itself just reports
        what it found.
        """
        idf = "Building, MyBuilding, 0;\nZone, ZoneOne;"
        facts = idf_facts.extract_poc_facts(idf)
        assert "idf_version" not in facts
        # Other facts still extracted.
        assert facts["north_axis_deg"] == 0.0
        assert facts["zone_count"] == 1

    def test_missing_building_omits_north_axis(self):
        """An IDF without a Building object omits north_axis_deg."""
        idf = "Version, 25.1;\nZone, ZoneOne;"
        facts = idf_facts.extract_poc_facts(idf)
        assert "north_axis_deg" not in facts
        assert facts["idf_version"] == "25.1"
        assert facts["zone_count"] == 1


# ── epJSON extraction ───────────────────────────────────────────────


class TestEpjsonExtraction:
    """The epJSON variant uses a JSON dict walker instead of regex.

    epJSON top-level structure is a dict keyed by object type, with
    each type's instances as a second-level dict. The parser walks
    this structure to extract the same three facts.

    Both formats must produce identical results for equivalent
    inputs — that's the test of cross-format parity.
    """

    def test_extracts_all_three_facts_from_epjson(self):
        """A complete epJSON dict produces the same three facts as IDF."""
        epjson = {
            "Version": {
                "Version 1": {"version_identifier": "25.1"},
            },
            "Building": {
                "My Building": {
                    "north_axis": 45.0,
                    "terrain": "Suburbs",
                },
            },
            "Zone": {
                "Zone One": {},
                "Zone Two": {},
            },
        }
        facts = idf_facts.extract_poc_facts(epjson)
        assert facts == {
            "idf_version": "25.1",
            "north_axis_deg": 45.0,
            "zone_count": 2,
        }

    def test_epjson_missing_building_falls_back(self):
        """epJSON without a Building dict simply omits north_axis_deg."""
        epjson = {
            "Version": {"V1": {"version_identifier": "25.1"}},
            "Zone": {"Z1": {}},
        }
        facts = idf_facts.extract_poc_facts(epjson)
        assert "north_axis_deg" not in facts
        assert facts["zone_count"] == 1

    def test_epjson_north_axis_default_when_field_absent(self):
        """epJSON Building without north_axis field defaults to 0.0.

        Matches the IDF behaviour — when the Building object exists
        but the field is missing, we apply the EnergyPlus default.
        """
        epjson = {
            "Building": {"My Building": {"terrain": "Suburbs"}},
        }
        facts = idf_facts.extract_poc_facts(epjson)
        assert facts["north_axis_deg"] == 0.0

    def test_epjson_zone_count_zero(self):
        """epJSON without a Zone dict reports zone_count=0."""
        epjson = {
            "Version": {"V1": {"version_identifier": "25.1"}},
        }
        facts = idf_facts.extract_poc_facts(epjson)
        assert facts["zone_count"] == 0


# ── Payload-type handling ───────────────────────────────────────────


class TestPayloadTypes:
    """Parser accepts str, bytes, dict — and refuses anything else.

    The validator pipeline may hand us the payload in any of these
    forms depending on the deployment and the preprocessing step.
    The parser normalises before parsing.
    """

    def test_bytes_payload_decoded(self):
        """bytes are decoded as UTF-8 before parsing."""
        idf_bytes = b"Version, 25.1;\nZone, ZoneOne;"
        facts = idf_facts.extract_poc_facts(idf_bytes)
        assert facts["idf_version"] == "25.1"
        assert facts["zone_count"] == 1

    def test_unrecognised_payload_type_returns_none(self):
        """Non-string, non-dict, non-bytes payloads return None.

        The validator then surfaces the result via on_missing on each
        signal — better than raising here (which would crash assertion
        evaluation rather than gracefully omitting values).
        """
        assert idf_facts.extract_poc_facts(12345) is None
        assert idf_facts.extract_poc_facts([1, 2, 3]) is None
        assert idf_facts.extract_poc_facts(None) is None

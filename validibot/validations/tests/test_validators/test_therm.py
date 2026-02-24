"""
Tests for the THERM validator.

TODO: Add comprehensive tests once the THERM implementation is
complete (parser data extraction, domain checks, signal extraction,
and validator integration).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from django.test import TestCase

from validibot.validations.validators.therm.geometry import compute_bounding_box
from validibot.validations.validators.therm.models import ThermPolygon
from validibot.validations.validators.therm.parser import parse_therm_file

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_THMX = FIXTURES_DIR / "sample_valid.thmx"


def _read_sample_thmx() -> str:
    return SAMPLE_THMX.read_text()


def _make_thmz(thmx_content: str) -> bytes:
    """Create a THMZ (ZIP) archive from THMX content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("model.thmx", thmx_content)
    return buf.getvalue()


# ---- Parser Tests ----


class ThermParserTests(TestCase):
    """Tests for parse_therm_file() format detection and error handling.

    TODO: Add data extraction tests once _build_model() is implemented.
    """

    def test_parse_thmx_format_detected(self):
        """Parser correctly identifies THMX format."""
        content = _read_sample_thmx()
        model = parse_therm_file(content, filename="test.thmx")
        assert model.source_format == "thmx"

    def test_parse_thmz_format_detected(self):
        """Parser correctly identifies THMZ (ZIP) format."""
        thmx = _read_sample_thmx()
        thmz = _make_thmz(thmx)
        model = parse_therm_file(thmz, filename="test.thmz")
        assert model.source_format == "thmz"

    def test_parse_thmz_auto_detect_zip(self):
        """THMZ auto-detected by ZIP magic bytes even without filename hint."""
        thmx = _read_sample_thmx()
        thmz = _make_thmz(thmx)
        model = parse_therm_file(thmz, filename=None)
        assert model.source_format == "thmz"

    def test_parse_invalid_xml(self):
        with pytest.raises(ValueError, match="Invalid XML"):
            parse_therm_file("<not valid xml<<<>>>")

    def test_parse_empty_content(self):
        with pytest.raises((ValueError, Exception)):
            parse_therm_file("")


# ---- Geometry Tests ----


class ThermGeometryTests(TestCase):
    """Tests for geometry utility functions.

    TODO: Add tests for run_geometry_checks() once implemented.
    """

    def test_compute_bounding_box(self):
        poly = ThermPolygon(
            id="1",
            material_id="mat",
            vertices=[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        )
        width, height = compute_bounding_box([poly])
        assert width == 10.0  # noqa: PLR2004
        assert height == 10.0  # noqa: PLR2004

    def test_compute_bounding_box_empty(self):
        width, height = compute_bounding_box([])
        assert width == 0.0
        assert height == 0.0

    def test_compute_bounding_box_multiple_polygons(self):
        p1 = ThermPolygon(
            id="1",
            material_id="m",
            vertices=[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        )
        p2 = ThermPolygon(
            id="2",
            material_id="m",
            vertices=[(10, 0), (30, 0), (30, 20), (10, 20), (10, 0)],
        )
        width, height = compute_bounding_box([p1, p2])
        assert width == 30.0  # noqa: PLR2004
        assert height == 20.0  # noqa: PLR2004

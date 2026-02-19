"""
Tests for the THERM validator engine.

Covers the parser, domain checks (geometry, materials, boundaries),
signal extraction, and full engine integration through the
SimpleValidatorEngine template method.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.test import TestCase

from validibot.submissions.constants import SubmissionFileType
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.engines.therm.boundaries import check_reference_integrity
from validibot.validations.engines.therm.engine import ThermValidatorEngine
from validibot.validations.engines.therm.geometry import all_polygons_closed
from validibot.validations.engines.therm.geometry import check_polygon_closure
from validibot.validations.engines.therm.geometry import compute_bounding_box
from validibot.validations.engines.therm.materials import check_material_properties
from validibot.validations.engines.therm.models import ThermBoundaryCondition
from validibot.validations.engines.therm.models import ThermMaterial
from validibot.validations.engines.therm.models import ThermMeshParameters
from validibot.validations.engines.therm.models import ThermModel
from validibot.validations.engines.therm.models import ThermPolygon
from validibot.validations.engines.therm.parser import parse_therm_file
from validibot.validations.engines.therm.signals import extract_signals
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory

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
    """Tests for parse_therm_file()."""

    def test_parse_valid_thmx(self):
        content = _read_sample_thmx()
        model = parse_therm_file(content, filename="test.thmx")

        assert model.source_format == "thmx"
        assert model.therm_version == "Version 8.0.20.0"
        assert len(model.polygons) == 3  # noqa: PLR2004
        assert len(model.materials) == 3  # noqa: PLR2004
        assert len(model.boundary_conditions) == 3  # noqa: PLR2004
        assert model.has_cma_data is True
        assert model.has_glazing_system is True

    def test_parse_polygon_vertices(self):
        content = _read_sample_thmx()
        model = parse_therm_file(content)

        poly1 = model.polygons[0]
        assert poly1.id == "1"
        assert poly1.material_id == "Aluminum"
        # 5 vertices (closed polygon: first == last)
        assert len(poly1.vertices) == 5  # noqa: PLR2004
        assert poly1.vertices[0] == (0.0, 0.0)
        assert poly1.vertices[-1] == (0.0, 0.0)

    def test_parse_materials(self):
        content = _read_sample_thmx()
        model = parse_therm_file(content)

        assert "Aluminum" in model.materials
        al = model.materials["Aluminum"]
        assert al.conductivity == 160.0  # noqa: PLR2004
        assert al.emissivity_outside == 0.2  # noqa: PLR2004

    def test_parse_boundary_conditions(self):
        content = _read_sample_thmx()
        model = parse_therm_file(content)

        assert "Interior Surface" in model.boundary_conditions
        interior = model.boundary_conditions["Interior Surface"]
        assert interior.bc_type == "interior"
        assert interior.temperature == 21.11  # noqa: PLR2004
        assert interior.film_coefficient == 8.14  # noqa: PLR2004

    def test_parse_ufactor_tags(self):
        content = _read_sample_thmx()
        model = parse_therm_file(content)

        tag_names = [t.name for t in model.ufactor_tags]
        assert "Frame" in tag_names
        assert "Edge" in tag_names

    def test_parse_mesh_params(self):
        content = _read_sample_thmx()
        model = parse_therm_file(content)

        assert model.mesh_params is not None
        assert model.mesh_params.mesh_level == 6  # noqa: PLR2004
        assert model.mesh_params.error_limit == 10.0  # noqa: PLR2004

    def test_parse_thmz_archive(self):
        thmx = _read_sample_thmx()
        thmz = _make_thmz(thmx)
        model = parse_therm_file(thmz, filename="test.thmz")

        assert model.source_format == "thmz"
        assert model.therm_version == "Version 8.0.20.0"
        assert len(model.polygons) == 3  # noqa: PLR2004

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

    def test_parse_no_namespace(self):
        """Parser handles THMX files without namespace."""
        xml = """<?xml version="1.0"?>
        <THERM-XML>
          <ThermVersion>7.0</ThermVersion>
          <Materials>
            <Material Name="Steel" Type="0" Conductivity="50.0" />
          </Materials>
          <Polygons />
          <BoundaryConditions />
        </THERM-XML>"""
        model = parse_therm_file(xml)
        assert model.therm_version == "7.0"
        assert "Steel" in model.materials


# ---- Geometry Tests ----


class ThermGeometryTests(TestCase):
    """Tests for geometry validation functions."""

    def _closed_polygon(self, poly_id="1", material="mat"):
        return ThermPolygon(
            id=poly_id,
            material_id=material,
            vertices=[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
        )

    def _unclosed_polygon(self, poly_id="1", material="mat"):
        return ThermPolygon(
            id=poly_id,
            material_id=material,
            vertices=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )

    def test_closed_polygon_passes(self):
        issues = check_polygon_closure([self._closed_polygon()])
        assert len(issues) == 0

    def test_unclosed_polygon_detected(self):
        issues = check_polygon_closure([self._unclosed_polygon()])
        assert len(issues) == 1
        assert issues[0].severity == Severity.ERROR
        assert "not closed" in issues[0].message

    def test_degenerate_polygon_detected(self):
        """Polygon with fewer than 3 vertices."""
        poly = ThermPolygon(
            id="1",
            material_id="mat",
            vertices=[(0, 0), (1, 1)],
        )
        issues = check_polygon_closure([poly])
        assert len(issues) == 1
        assert "fewer than" in issues[0].message

    def test_all_polygons_closed_true(self):
        assert all_polygons_closed([self._closed_polygon()]) is True

    def test_all_polygons_closed_false(self):
        assert all_polygons_closed([self._unclosed_polygon()]) is False

    def test_compute_bounding_box(self):
        poly = self._closed_polygon()
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


# ---- Material Tests ----


class ThermMaterialTests(TestCase):
    """Tests for material property validation."""

    def test_valid_materials_pass(self):
        mats = {
            "Steel": ThermMaterial(
                id="Steel",
                name="Steel",
                material_type="solid",
                conductivity=50.0,
                emissivity_inside=0.3,
                emissivity_outside=0.3,
            ),
        }
        issues = check_material_properties(mats)
        assert len(issues) == 0

    def test_zero_conductivity_error(self):
        mats = {
            "Bad": ThermMaterial(
                id="Bad",
                name="Bad",
                material_type="solid",
                conductivity=0.0,
            ),
        }
        issues = check_material_properties(mats)
        assert len(issues) == 1
        assert issues[0].severity == Severity.ERROR
        assert "positive" in issues[0].message

    def test_negative_conductivity_error(self):
        mats = {
            "Neg": ThermMaterial(
                id="Neg",
                name="Neg",
                material_type="solid",
                conductivity=-1.0,
            ),
        }
        issues = check_material_properties(mats)
        assert len(issues) == 1
        assert issues[0].severity == Severity.ERROR

    def test_out_of_range_conductivity_warning(self):
        mats = {
            "High": ThermMaterial(
                id="High",
                name="High",
                material_type="solid",
                conductivity=600.0,
            ),
        }
        issues = check_material_properties(mats)
        assert len(issues) == 1
        assert issues[0].severity == Severity.WARNING
        assert "outside" in issues[0].message

    def test_invalid_emissivity_warning(self):
        mats = {
            "Bad": ThermMaterial(
                id="Bad",
                name="Bad",
                material_type="solid",
                conductivity=1.0,
                emissivity_inside=1.5,
            ),
        }
        issues = check_material_properties(mats)
        assert len(issues) == 1
        assert issues[0].severity == Severity.WARNING


# ---- Boundary Tests ----


class ThermBoundaryTests(TestCase):
    """Tests for reference integrity validation."""

    def test_valid_references_pass(self):
        polys = [ThermPolygon(id="1", material_id="Steel", vertices=[])]
        mats = {
            "Steel": ThermMaterial(
                id="Steel",
                name="Steel",
                material_type="solid",
                conductivity=50.0,
            ),
        }
        bcs: dict[str, ThermBoundaryCondition] = {}
        issues = check_reference_integrity(polys, mats, bcs)
        # No dangling refs; Steel is referenced by polygon 1
        error_issues = [i for i in issues if i.severity == Severity.ERROR]
        assert len(error_issues) == 0

    def test_dangling_material_ref_error(self):
        polys = [ThermPolygon(id="1", material_id="Missing", vertices=[])]
        mats: dict[str, ThermMaterial] = {}
        bcs: dict[str, ThermBoundaryCondition] = {}
        issues = check_reference_integrity(polys, mats, bcs)
        error_issues = [i for i in issues if i.severity == Severity.ERROR]
        assert len(error_issues) == 1
        assert "not defined" in error_issues[0].message

    def test_orphaned_material_warning(self):
        polys = [ThermPolygon(id="1", material_id="Steel", vertices=[])]
        mats = {
            "Steel": ThermMaterial(
                id="Steel",
                name="Steel",
                material_type="solid",
                conductivity=50.0,
            ),
            "Unused": ThermMaterial(
                id="Unused",
                name="Unused",
                material_type="solid",
                conductivity=1.0,
            ),
        }
        bcs: dict[str, ThermBoundaryCondition] = {}
        issues = check_reference_integrity(polys, mats, bcs)
        warnings = [i for i in issues if i.severity == Severity.WARNING]
        assert len(warnings) == 1
        assert "Unused" in warnings[0].message


# ---- Signal Extraction Tests ----


class ThermSignalTests(TestCase):
    """Tests for signal extraction from ThermModel."""

    def _make_model(self) -> ThermModel:
        return ThermModel(
            source_format="thmx",
            therm_version="8.0",
            polygons=[
                ThermPolygon(
                    id="1",
                    material_id="Al",
                    vertices=[(0, 0), (50, 0), (50, 100), (0, 100), (0, 0)],
                ),
            ],
            materials={
                "Al": ThermMaterial(
                    id="Al",
                    name="Aluminum",
                    material_type="solid",
                    conductivity=160.0,
                ),
            },
            boundary_conditions={
                "Interior": ThermBoundaryCondition(
                    id="Interior",
                    name="Interior",
                    bc_type="interior",
                    temperature=21.11,
                    film_coefficient=8.14,
                ),
                "Exterior": ThermBoundaryCondition(
                    id="Exterior",
                    name="Exterior",
                    bc_type="exterior",
                    temperature=-17.78,
                    film_coefficient=26.0,
                ),
            },
            mesh_params=ThermMeshParameters(mesh_level=6),
            has_cma_data=True,
            has_glazing_system=False,
        )

    def test_signal_keys(self):
        signals = extract_signals(self._make_model())
        expected_keys = {
            "polygon_count",
            "material_count",
            "bc_count",
            "geometry_width_mm",
            "geometry_height_mm",
            "all_polygons_closed",
            "interior_bc_temp",
            "exterior_bc_temp",
            "interior_film_coeff",
            "exterior_film_coeff",
            "ufactor_tags_found",
            "mesh_level",
            "has_cma_data",
            "has_glazing_system",
            "therm_version",
        }
        assert set(signals.keys()) == expected_keys

    def test_signal_values(self):
        signals = extract_signals(self._make_model())
        assert signals["polygon_count"] == 1
        assert signals["material_count"] == 1
        assert signals["bc_count"] == 2  # noqa: PLR2004
        assert signals["geometry_width_mm"] == 50.0  # noqa: PLR2004
        assert signals["geometry_height_mm"] == 100.0  # noqa: PLR2004
        assert signals["all_polygons_closed"] is True
        assert signals["interior_bc_temp"] == 21.11  # noqa: PLR2004
        assert signals["exterior_bc_temp"] == -17.78  # noqa: PLR2004
        assert signals["interior_film_coeff"] == 8.14  # noqa: PLR2004
        assert signals["exterior_film_coeff"] == 26.0  # noqa: PLR2004
        assert signals["mesh_level"] == 6  # noqa: PLR2004
        assert signals["has_cma_data"] is True
        assert signals["has_glazing_system"] is False
        assert signals["therm_version"] == "8.0"

    def test_signals_empty_model(self):
        model = ThermModel(source_format="thmx", therm_version=None)
        signals = extract_signals(model)
        assert signals["polygon_count"] == 0
        assert signals["interior_bc_temp"] is None
        assert signals["mesh_level"] is None


# ---- Engine Integration Tests ----


class ThermEngineIntegrationTests(TestCase):
    """Tests for the full ThermValidatorEngine via SimpleValidatorEngine."""

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.THERM,
            name="THERM Validator",
        )
        cls.ruleset = RulesetFactory(
            org=cls.org,
            ruleset_type=RulesetType.THERM,
        )

    def _make_xml_submission(self, content: str) -> MagicMock:
        """Create a mock submission for THMX files."""
        sub = MagicMock()
        sub.file_type = SubmissionFileType.XML
        sub.get_content.return_value = content
        sub.original_filename = "test.thmx"
        sub.input_file = None
        return sub

    def test_valid_thmx_passes(self):
        engine = ThermValidatorEngine()
        sub = self._make_xml_submission(_read_sample_thmx())
        result = engine.validate(self.validator, sub, self.ruleset)

        assert result.passed is True
        assert result.signals is not None
        assert result.signals["polygon_count"] == 3  # noqa: PLR2004

    def test_wrong_file_type_rejected(self):
        engine = ThermValidatorEngine()
        sub = MagicMock()
        sub.file_type = SubmissionFileType.JSON
        result = engine.validate(self.validator, sub, self.ruleset)

        assert result.passed is False
        assert len(result.issues) == 1
        assert result.issues[0].severity == Severity.ERROR

    def test_invalid_xml_fails(self):
        engine = ThermValidatorEngine()
        sub = self._make_xml_submission("<broken<<<")
        result = engine.validate(self.validator, sub, self.ruleset)

        assert result.passed is False
        assert any("parse" in i.message.lower() for i in result.issues)

    def test_empty_content_fails(self):
        engine = ThermValidatorEngine()
        sub = self._make_xml_submission("")
        result = engine.validate(self.validator, sub, self.ruleset)

        assert result.passed is False

    def test_engine_registered(self):
        """Verify the engine is registered in the global registry."""
        from validibot.validations.engines import registry

        engine_cls = registry.get(ValidationType.THERM)
        assert engine_cls is ThermValidatorEngine

    def test_signals_available_in_result(self):
        engine = ThermValidatorEngine()
        sub = self._make_xml_submission(_read_sample_thmx())
        result = engine.validate(self.validator, sub, self.ruleset)

        assert result.signals is not None
        assert "interior_bc_temp" in result.signals
        assert result.signals["interior_bc_temp"] == 21.11  # noqa: PLR2004
        assert result.signals["therm_version"] == "Version 8.0.20.0"

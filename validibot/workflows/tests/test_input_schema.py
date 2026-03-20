"""Tests for the schema-driven workflow input contract feature.

This test suite covers the full authoring-to-launch pipeline for
structured input forms on JSON-only workflows:

- **schema_builder**: Building Pydantic models from JSON Schema, eligibility gate.
- **form_builder**: Generating Django forms from JSON Schema, requirement rows.
- **schema_authoring**: Parsing JSON Schema and restricted Pydantic text, security
  enforcement, resource limits, and error reporting.

These components together implement ADR 2026-03-19 (Schema-Driven Workflow Input
Contracts for JSON Workflows).
"""

from __future__ import annotations

import json
import textwrap

import pytest
from django.core.exceptions import ValidationError
from pydantic import ValidationError as PydanticValidationError

from validibot.submissions.constants import SubmissionFileType
from validibot.workflows.form_builder import schema_to_django_form
from validibot.workflows.form_builder import schema_to_requirement_rows
from validibot.workflows.schema_authoring import parse_json_schema_input
from validibot.workflows.schema_authoring import parse_pydantic_input
from validibot.workflows.schema_authoring import validate_schema_subset
from validibot.workflows.schema_builder import build_pydantic_model
from validibot.workflows.schema_builder import workflow_has_input_form
from validibot.workflows.views.launch import WorkflowLaunchDetailView

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def section_j_schema():
    """A realistic Section J DTS pre-check schema used across multiple tests.

    This exercises integer enums, float constraints with exclusive bounds,
    units via json_schema_extra, and a mix of required and constrained fields.
    """
    return {
        "title": "Section J DTS Pre-Check",
        "type": "object",
        "properties": {
            "climate_zone": {
                "type": "integer",
                "description": "NCC Climate Zone",
                "minimum": 1,
                "maximum": 8,
            },
            "building_class": {
                "type": "integer",
                "description": "NCC Building Classification",
                "enum": [2, 3, 5, 6, 7, 8, 9],
            },
            "wall_r_value": {
                "type": "number",
                "description": "Total wall R-value",
                "exclusiveMinimum": 0,
                "maximum": 10,
                "units": "m²K/W",
            },
            "glazing_shgc": {
                "type": "number",
                "description": "Solar Heat Gain Coefficient",
                "minimum": 0,
                "maximum": 1,
            },
        },
        "required": ["climate_zone", "building_class", "wall_r_value", "glazing_shgc"],
    }


@pytest.fixture
def simple_schema():
    """A minimal schema with one required string field.

    Useful as a baseline for tests that don't need complex constraints.
    """
    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Your name",
            },
        },
        "required": ["name"],
    }


# ═══════════════════════════════════════════════════════════════════════════
# schema_builder tests — Pydantic model construction and eligibility
# ═══════════════════════════════════════════════════════════════════════════


class TestWorkflowHasInputForm:
    """The eligibility gate controls whether the form tab appears.

    A workflow qualifies only when allowed_file_types is exactly ["json"]
    AND input_schema has at least one property.
    """

    def test_eligible_json_only_with_schema(self, section_j_schema):
        """JSON-only workflow with a populated schema is eligible."""

        class FakeWorkflow:
            allowed_file_types = [SubmissionFileType.JSON]
            input_schema = section_j_schema

        assert workflow_has_input_form(FakeWorkflow()) is True

    def test_ineligible_no_schema(self):
        """JSON-only workflow without a schema is ineligible."""

        class FakeWorkflow:
            allowed_file_types = [SubmissionFileType.JSON]
            input_schema = None

        assert workflow_has_input_form(FakeWorkflow()) is False

    def test_ineligible_empty_properties(self):
        """Schema with empty properties dict is ineligible."""

        class FakeWorkflow:
            allowed_file_types = [SubmissionFileType.JSON]
            input_schema = {"type": "object", "properties": {}}

        assert workflow_has_input_form(FakeWorkflow()) is False

    def test_ineligible_non_json_file_types(self, section_j_schema):
        """Workflow accepting XML is ineligible even with a schema."""

        class FakeWorkflow:
            allowed_file_types = [SubmissionFileType.JSON, SubmissionFileType.XML]
            input_schema = section_j_schema

        assert workflow_has_input_form(FakeWorkflow()) is False

    def test_ineligible_xml_only(self, section_j_schema):
        """XML-only workflow is ineligible regardless of schema."""

        class FakeWorkflow:
            allowed_file_types = [SubmissionFileType.XML]
            input_schema = section_j_schema

        assert workflow_has_input_form(FakeWorkflow()) is False


class TestBuildPydanticModel:
    """Construct runtime Pydantic validators from stored JSON Schema.

    The Pydantic model is a derived runtime adapter — the stored JSON Schema
    remains the canonical contract.
    """

    def test_all_field_types(self):
        """All four supported JSON Schema types map to the correct Python types."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
                "weight": {"type": "number"},
                "active": {"type": "boolean"},
            },
            "required": ["name", "age", "weight", "active"],
        }
        model = build_pydantic_model(schema)
        expected_age = 30
        expected_weight = 65.5
        instance = model(
            name="Alice",
            age=expected_age,
            weight=expected_weight,
            active=True,
        )
        assert instance.name == "Alice"
        assert instance.age == expected_age
        assert instance.weight == expected_weight
        assert instance.active is True

    def test_numeric_constraints(self, section_j_schema):
        """Min/max and exclusive bounds are enforced by the Pydantic model."""
        model = build_pydantic_model(section_j_schema)

        # Valid input
        model(climate_zone=3, building_class=5, wall_r_value=2.5, glazing_shgc=0.4)

        # Climate zone below minimum (1)
        with pytest.raises(PydanticValidationError):
            model(climate_zone=0, building_class=5, wall_r_value=2.5, glazing_shgc=0.4)

        # Climate zone above maximum (8)
        with pytest.raises(PydanticValidationError):
            model(climate_zone=9, building_class=5, wall_r_value=2.5, glazing_shgc=0.4)

        # wall_r_value at exclusive minimum (0) — should fail
        with pytest.raises(PydanticValidationError):
            model(climate_zone=3, building_class=5, wall_r_value=0, glazing_shgc=0.4)

    def test_enum_constraint(self, section_j_schema):
        """Enum values produce Literal types that reject non-members."""
        model = build_pydantic_model(section_j_schema)

        # Valid enum value
        model(climate_zone=3, building_class=5, wall_r_value=2.5, glazing_shgc=0.4)

        # Invalid enum value (4 is not in [2, 3, 5, 6, 7, 8, 9])
        with pytest.raises(PydanticValidationError):
            model(climate_zone=3, building_class=4, wall_r_value=2.5, glazing_shgc=0.4)

    def test_optional_field_with_default(self):
        """Optional fields with defaults don't need to be provided."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "color": {"type": "string", "default": "blue"},
            },
            "required": ["name"],
        }
        model = build_pydantic_model(schema)
        instance = model(name="Test")
        assert instance.color == "blue"

    def test_optional_field_without_default(self):
        """Optional fields without defaults default to None."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["name"],
        }
        model = build_pydantic_model(schema)
        instance = model(name="Test")
        assert instance.note is None

    def test_missing_required_field_raises(self, simple_schema):
        """Missing required fields cause PydanticValidationError."""
        model = build_pydantic_model(simple_schema)
        with pytest.raises(PydanticValidationError):
            model()

    def test_units_preserved_in_json_schema_extra(self):
        """Units from the stored schema are preserved in json_schema_extra."""
        schema = {
            "type": "object",
            "properties": {
                "temperature": {
                    "type": "number",
                    "units": "°C",
                },
            },
            "required": ["temperature"],
        }
        model = build_pydantic_model(schema)
        json_schema = model.model_json_schema()
        assert json_schema["properties"]["temperature"]["units"] == "°C"


# ═══════════════════════════════════════════════════════════════════════════
# form_builder tests — Django form generation and requirement rows
# ═══════════════════════════════════════════════════════════════════════════


class TestSchemaToDjangoForm:
    """Convert JSON Schema to Django Form classes with correct field types.

    This tests the presentation layer — the Django form is a UX adapter,
    not the authoritative validation (that's the Pydantic layer).
    """

    def test_basic_field_types(self):
        """All four JSON Schema types produce the correct Django field types."""
        from django import forms as django_forms

        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "enabled": {"type": "boolean"},
            },
            "required": ["name", "count", "ratio", "enabled"],
        }
        form_class = schema_to_django_form(schema)
        form = form_class()
        assert isinstance(form.fields["name"], django_forms.CharField)
        assert isinstance(form.fields["count"], django_forms.IntegerField)
        assert isinstance(form.fields["ratio"], django_forms.FloatField)
        assert isinstance(form.fields["enabled"], django_forms.BooleanField)

    def test_enum_uses_typed_choice_field(self):
        """Integer enums produce TypedChoiceField with int coercion.

        This prevents the Django ChoiceField string coercion problem where
        '2' != Literal[2, 3, 5] at the Pydantic validation layer.
        """
        from django import forms as django_forms

        schema = {
            "type": "object",
            "properties": {
                "building_class": {
                    "type": "integer",
                    "enum": [2, 3, 5],
                },
            },
            "required": ["building_class"],
        }
        form_class = schema_to_django_form(schema)
        form = form_class()
        field = form.fields["building_class"]
        assert isinstance(field, django_forms.TypedChoiceField)

        # Verify coercion produces an int, not a string
        expected_value = 5
        form2 = form_class(data={"building_class": "5"})
        assert form2.is_valid()
        assert form2.cleaned_data["building_class"] == expected_value
        assert isinstance(form2.cleaned_data["building_class"], int)

    def test_numeric_constraints_at_construction(self):
        """Numeric min/max are passed as constructor kwargs.

        Django only installs MinValueValidator/MaxValueValidator during
        __init__, not when you assign min_value/max_value post-construction.
        This test verifies the constraints actually work.
        """
        schema = {
            "type": "object",
            "properties": {
                "temperature": {
                    "type": "number",
                    "minimum": -273.15,
                    "maximum": 1000,
                },
            },
            "required": ["temperature"],
        }
        form_class = schema_to_django_form(schema)

        # Valid value
        form = form_class(data={"temperature": "20.5"})
        assert form.is_valid()

        # Below minimum
        form = form_class(data={"temperature": "-300"})
        assert not form.is_valid()

        # Above maximum
        form = form_class(data={"temperature": "1001"})
        assert not form.is_valid()

    def test_exclusive_integer_bounds(self):
        """Exclusive bounds on integers are converted to inclusive.

        exclusiveMinimum: 0 -> min_value: 1 for integers.
        """
        schema = {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "exclusiveMinimum": 0,
                    "exclusiveMaximum": 10,
                },
            },
            "required": ["count"],
        }
        form_class = schema_to_django_form(schema)

        # 0 is excluded (exclusive min)
        form = form_class(data={"count": "0"})
        assert not form.is_valid()

        # 1 is valid (min_value: 1)
        form = form_class(data={"count": "1"})
        assert form.is_valid()

        # 10 is excluded (exclusive max -> max_value: 9)
        form = form_class(data={"count": "10"})
        assert not form.is_valid()

        # 9 is valid (max_value: 9)
        form = form_class(data={"count": "9"})
        assert form.is_valid()

    def test_help_text_with_units(self):
        """Help text combines description and units."""
        schema = {
            "type": "object",
            "properties": {
                "r_value": {
                    "type": "number",
                    "description": "Wall R-value",
                    "units": "m²K/W",
                },
            },
            "required": ["r_value"],
        }
        form_class = schema_to_django_form(schema)
        form = form_class()
        assert "Wall R-value" in str(form.fields["r_value"].help_text)
        assert "m²K/W" in str(form.fields["r_value"].help_text)

    def test_default_value_sets_initial(self):
        """Default values from the schema set the form field's initial."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "default": "Anonymous"},
            },
            "required": [],
        }
        form_class = schema_to_django_form(schema)
        form = form_class()
        assert form.fields["name"].initial == "Anonymous"

    def test_crispy_helper_attached(self):
        """The generated form has a crispy FormHelper with form_tag=False."""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        form_class = schema_to_django_form(schema)
        form = form_class()
        assert hasattr(form, "helper")
        assert form.helper.form_tag is False
        assert form.helper.disable_csrf is True


class TestSchemaToRequirementRows:
    """Generate human-readable requirement rows for display.

    These rows drive the requirements table on the launch page,
    the public info page, and the authoring preview modal.
    """

    def test_basic_row_generation(self, section_j_schema):
        """All schema properties produce correctly-shaped rows."""
        rows = schema_to_requirement_rows(section_j_schema)
        expected_row_count = 4
        assert len(rows) == expected_row_count

        cz_row = next(r for r in rows if r["name"] == "climate_zone")
        assert cz_row["type_label"] == "Integer"
        assert cz_row["required"] is True
        assert "1" in cz_row["constraints"]
        assert "8" in cz_row["constraints"]

    def test_enum_values_in_row(self, section_j_schema):
        """Enum properties include the allowed values list."""
        rows = schema_to_requirement_rows(section_j_schema)
        bc_row = next(r for r in rows if r["name"] == "building_class")
        assert bc_row["enum_values"] == [2, 3, 5, 6, 7, 8, 9]

    def test_units_in_row(self, section_j_schema):
        """Units are passed through to the row."""
        rows = schema_to_requirement_rows(section_j_schema)
        wr_row = next(r for r in rows if r["name"] == "wall_r_value")
        assert wr_row["units"] == "m²K/W"

    def test_exclusive_bounds_in_constraints(self, section_j_schema):
        """Exclusive bounds show > instead of >=."""
        rows = schema_to_requirement_rows(section_j_schema)
        wr_row = next(r for r in rows if r["name"] == "wall_r_value")
        assert "> 0" in wr_row["constraints"]

    def test_default_in_row(self):
        """Default values appear in the row."""
        schema = {
            "type": "object",
            "properties": {
                "color": {"type": "string", "default": "blue"},
            },
            "required": [],
        }
        rows = schema_to_requirement_rows(schema)
        assert rows[0]["default"] == "blue"


# ═══════════════════════════════════════════════════════════════════════════
# schema_authoring tests — JSON Schema and Pydantic text parsing
# ═══════════════════════════════════════════════════════════════════════════


class TestParseJsonSchemaInput:
    """Parse and validate author-provided JSON Schema text.

    Authoring validation happens at create/edit time so submitters
    never encounter authoring mistakes at launch time.
    """

    def test_valid_schema(self, section_j_schema):
        """Valid JSON Schema parses without error."""
        text = json.dumps(section_j_schema)
        result = parse_json_schema_input(text)
        assert result["properties"]["climate_zone"]["type"] == "integer"

    def test_invalid_json_syntax(self):
        """JSON syntax errors report line and column."""
        with pytest.raises(ValidationError) as exc_info:
            parse_json_schema_input('{"broken": }')
        assert "invalid_json" in str(exc_info.value.code) or "Invalid JSON" in str(
            exc_info.value.message
        )

    def test_not_an_object(self):
        """Non-object JSON is rejected."""
        with pytest.raises(ValidationError):
            parse_json_schema_input('"just a string"')

    def test_unsupported_type_rejected(self):
        """Properties with unsupported types (e.g. 'array') are rejected."""
        schema = {
            "type": "object",
            "properties": {
                "items": {"type": "array"},
            },
        }
        with pytest.raises(ValidationError) as exc_info:
            parse_json_schema_input(json.dumps(schema))
        assert "unsupported_type" in str(exc_info.value.code)

    def test_nested_objects_rejected(self):
        """Nested object properties are rejected in v1.

        The ``properties`` key inside a property dict is not in
        SUPPORTED_PROPERTY_KEYS, so the unsupported-key check fires
        before the type check.
        """
        schema = {
            "type": "object",
            "properties": {
                "address": {
                    "type": "object",
                    "properties": {"street": {"type": "string"}},
                },
            },
        }
        with pytest.raises(ValidationError) as exc_info:
            parse_json_schema_input(json.dumps(schema))
        assert exc_info.value.code in (
            "unsupported_type",
            "unsupported_property_keys",
        )

    def test_schema_composition_rejected(self):
        """$ref, allOf, oneOf, anyOf keywords are rejected.

        These are caught by the unsupported-property-keys check since
        composition keywords are not in SUPPORTED_PROPERTY_KEYS.
        """
        schema = {
            "type": "object",
            "properties": {
                "value": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            },
        }
        with pytest.raises(ValidationError):
            parse_json_schema_input(json.dumps(schema))

    def test_optional_boolean_without_default_rejected(self):
        """Optional booleans without explicit defaults are rejected.

        Checkbox UX collapses 'absent' and False, which is lossy
        relative to the canonical contract.
        """
        schema = {
            "type": "object",
            "properties": {
                "flag": {"type": "boolean"},
            },
            "required": [],
        }
        with pytest.raises(ValidationError) as exc_info:
            parse_json_schema_input(json.dumps(schema))
        assert "optional_boolean" in str(exc_info.value.code)

    def test_required_boolean_allowed(self):
        """Required booleans are allowed (no lossy absent/false issue)."""
        schema = {
            "type": "object",
            "properties": {
                "flag": {"type": "boolean"},
            },
            "required": ["flag"],
        }
        result = parse_json_schema_input(json.dumps(schema))
        assert result["properties"]["flag"]["type"] == "boolean"

    def test_optional_boolean_with_default_allowed(self):
        """Optional booleans with explicit defaults are allowed."""
        schema = {
            "type": "object",
            "properties": {
                "flag": {"type": "boolean", "default": False},
            },
            "required": [],
        }
        result = parse_json_schema_input(json.dumps(schema))
        assert result["properties"]["flag"]["default"] is False


class TestParsePydanticInput:
    """Parse restricted Pydantic 2 class text into canonical JSON Schema.

    Security: the parser uses ast.parse() only — never code execution.
    Unknown AST constructs cause rejection, not sanitization.
    """

    def test_basic_fields(self):
        """Simple field types produce correct JSON Schema properties."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                name: str
                age: int
                weight: float
                active: bool
        """)
        schema = parse_pydantic_input(text)
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["age"]["type"] == "integer"
        assert schema["properties"]["weight"]["type"] == "number"
        assert schema["properties"]["active"]["type"] == "boolean"
        assert set(schema["required"]) == {"name", "age", "weight", "active"}

    def test_field_with_description_and_constraints(self):
        """Field() kwargs map to JSON Schema properties."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                temperature: float = Field(
                    description="Temperature reading",
                    ge=-273.15,
                    le=1000,
                )
        """)
        schema = parse_pydantic_input(text)
        prop = schema["properties"]["temperature"]
        expected_min = -273.15
        expected_max = 1000
        assert prop["description"] == "Temperature reading"
        assert prop["minimum"] == expected_min
        assert prop["maximum"] == expected_max

    def test_field_with_units_via_json_schema_extra(self):
        """json_schema_extra={"units": ...} flattens onto the property."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                r_value: float = Field(
                    description="R-value",
                    json_schema_extra={"units": "m²K/W"},
                )
        """)
        schema = parse_pydantic_input(text)
        assert schema["properties"]["r_value"]["units"] == "m²K/W"

    def test_optional_field(self):
        """Optional[T] fields are not required."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                name: str
                note: Optional[str]
        """)
        schema = parse_pydantic_input(text)
        assert "name" in schema.get("required", [])
        assert "note" not in schema.get("required", [])

    def test_literal_enum(self):
        """Literal[...] produces an enum constraint."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                zone: Literal[1, 2, 3]
        """)
        schema = parse_pydantic_input(text)
        assert schema["properties"]["zone"]["enum"] == [1, 2, 3]
        assert schema["properties"]["zone"]["type"] == "integer"

    def test_default_value(self):
        """Fields with defaults produce non-required properties."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                color: str = "blue"
        """)
        schema = parse_pydantic_input(text)
        assert schema["properties"]["color"]["default"] == "blue"
        assert "color" not in schema.get("required", [])

    def test_class_title_from_name(self):
        """The class name becomes the schema title."""
        text = textwrap.dedent("""\
            class SectionJInput(BaseModel):
                value: int
        """)
        schema = parse_pydantic_input(text)
        assert schema["title"] == "SectionJInput"

    def test_docstring_allowed(self):
        """Class docstrings are allowed (and ignored)."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                \"\"\"This is a docstring.\"\"\"
                value: int
        """)
        schema = parse_pydantic_input(text)
        assert "value" in schema["properties"]

    # ── Security: rejection of disallowed constructs ─────────────────

    def test_methods_rejected(self):
        """Methods in the class body are rejected."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                name: str
                def validate_name(self):
                    pass
        """)
        with pytest.raises(ValidationError) as exc_info:
            parse_pydantic_input(text)
        assert "disallowed_statement" in str(exc_info.value.code)

    def test_validators_rejected(self):
        """@validator decorators are rejected (they're FunctionDef nodes)."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                name: str
                @validator("name")
                def check_name(cls, v):
                    return v
        """)
        with pytest.raises(ValidationError) as exc_info:
            parse_pydantic_input(text)
        assert "disallowed_statement" in str(exc_info.value.code)

    def test_nested_models_rejected(self):
        """Nested model references are rejected (unsupported type)."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                name: str
                address: Address
        """)
        with pytest.raises(ValidationError):
            parse_pydantic_input(text)

    def test_dangerous_call_expression_rejected(self):
        """Arbitrary function calls as defaults are rejected.

        This is the core security boundary: we must not allow
        arbitrary callables to pass through.
        """
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                x: int = dangerous()
        """)
        with pytest.raises(ValidationError) as exc_info:
            parse_pydantic_input(text)
        assert "disallowed_call" in str(exc_info.value.code)

    def test_default_factory_rejected(self):
        """Field(default_factory=...) is explicitly rejected."""
        text = textwrap.dedent("""\
            class MyInput(BaseModel):
                items: str = Field(default_factory=list)
        """)
        with pytest.raises(ValidationError) as exc_info:
            parse_pydantic_input(text)
        assert "default_factory" in str(exc_info.value.code)

    def test_top_level_assignment_rejected(self):
        """Top-level statements other than imports and class are rejected."""
        text = textwrap.dedent("""\
            x = 42
            class MyInput(BaseModel):
                name: str
        """)
        with pytest.raises(ValidationError) as exc_info:
            parse_pydantic_input(text)
        assert "disallowed_top_level" in str(exc_info.value.code)

    def test_imports_allowed(self):
        """Import statements at top level are harmless and allowed."""
        text = textwrap.dedent("""\
            from pydantic import BaseModel, Field
            from typing import Optional, Literal

            class MyInput(BaseModel):
                name: str
        """)
        schema = parse_pydantic_input(text)
        assert "name" in schema["properties"]

    def test_multiple_classes_rejected(self):
        """Only exactly one class definition is allowed."""
        text = textwrap.dedent("""\
            class A(BaseModel):
                x: int
            class B(BaseModel):
                y: int
        """)
        with pytest.raises(ValidationError) as exc_info:
            parse_pydantic_input(text)
        assert "wrong_class_count" in str(exc_info.value.code)

    def test_non_basemodel_rejected(self):
        """Classes not inheriting from BaseModel are rejected."""
        text = textwrap.dedent("""\
            class MyInput(SomethingElse):
                name: str
        """)
        with pytest.raises(ValidationError) as exc_info:
            parse_pydantic_input(text)
        assert "not_basemodel" in str(exc_info.value.code)

    def test_syntax_error_reported(self):
        """Python syntax errors are reported with line numbers."""
        text = "class MyInput(BaseModel:\n  name: str"
        with pytest.raises(ValidationError) as exc_info:
            parse_pydantic_input(text)
        assert "syntax_error" in str(exc_info.value.code)


class TestResourceLimits:
    """Resource limits prevent parser denial-of-service."""

    def test_oversized_input_rejected(self):
        """Input exceeding MAX_SCHEMA_CHARS is rejected."""
        text = "x" * 25_000
        with pytest.raises(ValidationError) as exc_info:
            parse_json_schema_input(text)
        assert "too_large" in str(exc_info.value.code)

    def test_too_many_lines_rejected(self):
        """Input exceeding MAX_SCHEMA_LINES is rejected."""
        text = "\n".join([f'"line{i}": {i}' for i in range(600)])
        with pytest.raises(ValidationError) as exc_info:
            parse_json_schema_input(text)
        assert "too_many_lines" in str(exc_info.value.code)


# ═══════════════════════════════════════════════════════════════════════════
# validate_schema_subset tests — schema constraint enforcement
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateSchemaSubset:
    """Validate that schemas conform to the v1 supported subset."""

    def test_missing_type_object(self):
        """Schema without type: object is rejected."""
        with pytest.raises(ValidationError):
            validate_schema_subset({"properties": {"x": {"type": "string"}}})

    def test_missing_properties(self):
        """Schema without properties key is rejected."""
        with pytest.raises(ValidationError):
            validate_schema_subset({"type": "object"})

    def test_bad_required_type(self):
        """Non-list required field is rejected."""
        with pytest.raises(ValidationError):
            validate_schema_subset(
                {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": "x",
                }
            )


# ═══════════════════════════════════════════════════════════════════════════
# Integration: end-to-end schema -> Pydantic -> Django form
# ═══════════════════════════════════════════════════════════════════════════


class TestEndToEndSchemaFlow:
    """Verify the full pipeline from JSON Schema to validated form submission.

    This exercises the same path used at launch time: JSON Schema -> Django
    form -> cleaned data -> Pydantic validation.
    """

    def test_form_to_pydantic_round_trip(self, section_j_schema):
        """Valid form data passes both Django and Pydantic validation."""
        form_class = schema_to_django_form(section_j_schema)
        form = form_class(
            data={
                "climate_zone": "3",
                "building_class": "5",
                "wall_r_value": "2.5",
                "glazing_shgc": "0.4",
            }
        )
        assert form.is_valid(), f"Form errors: {form.errors}"

        # Coerced values should pass Pydantic
        pydantic_model = build_pydantic_model(section_j_schema)
        cleaned = {k: v for k, v in form.cleaned_data.items() if v is not None}
        instance = pydantic_model(**cleaned)
        expected_zone = 3
        expected_class = 5
        expected_r_value = 2.5
        assert instance.climate_zone == expected_zone
        assert instance.building_class == expected_class
        assert instance.wall_r_value == expected_r_value

    def test_integer_enum_coercion(self, section_j_schema):
        """TypedChoiceField coerces string '5' to int 5 for Pydantic Literal."""
        form_class = schema_to_django_form(section_j_schema)
        form = form_class(
            data={
                "climate_zone": "3",
                "building_class": "5",
                "wall_r_value": "2.5",
                "glazing_shgc": "0.4",
            }
        )
        assert form.is_valid()
        # This would fail with plain ChoiceField (returns '5' not 5)
        assert isinstance(form.cleaned_data["building_class"], int)

    def test_pydantic_text_to_form_round_trip(self):
        """Pydantic text -> JSON Schema -> Django form -> Pydantic validation."""
        text = textwrap.dedent("""\
            class TestInput(BaseModel):
                count: int = Field(description="Item count", ge=1, le=100)
                label: str = Field(description="Label", default="untitled")
        """)
        schema = parse_pydantic_input(text)
        form_class = schema_to_django_form(schema)
        form = form_class(data={"count": "42"})
        assert form.is_valid(), f"Form errors: {form.errors}"

        pydantic_model = build_pydantic_model(schema)
        cleaned = {
            k: v for k, v in form.cleaned_data.items() if v is not None and v != ""
        }
        instance = pydantic_model(**cleaned)
        expected_count = 42
        assert instance.count == expected_count
        assert instance.label == "untitled"


# ═══════════════════════════════════════════════════════════════════════════
# Unsupported property key rejection — the contract must not promise more
# than the runtime can enforce
# ═══════════════════════════════════════════════════════════════════════════


class TestUnsupportedPropertyKeys:
    """Verify that property keys not in SUPPORTED_PROPERTY_KEYS are rejected.

    If a keyword like ``pattern`` or ``minLength`` is accepted into the
    stored schema but neither the Django form nor the Pydantic model
    builder reads it, users get false confidence that the constraint is
    being enforced.  The validator must reject these at authoring time.
    """

    def test_pattern_rejected(self):
        """The ``pattern`` keyword is not supported and must be rejected."""
        schema = {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "pattern": r"^[^@]+@[^@]+$",
                },
            },
        }
        with pytest.raises(ValidationError) as exc_info:
            parse_json_schema_input(json.dumps(schema))
        assert "unsupported_property_keys" in str(exc_info.value.code)

    def test_min_length_rejected(self):
        """The ``minLength`` keyword is not supported and must be rejected."""
        schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                },
            },
        }
        with pytest.raises(ValidationError) as exc_info:
            parse_json_schema_input(json.dumps(schema))
        assert "unsupported_property_keys" in str(exc_info.value.code)

    def test_multiple_unsupported_keys_listed(self):
        """Error message lists all unsupported keys, not just the first."""
        schema = {
            "type": "object",
            "properties": {
                "value": {
                    "type": "integer",
                    "multipleOf": 5,
                    "format": "int32",
                },
            },
        }
        with pytest.raises(ValidationError) as exc_info:
            parse_json_schema_input(json.dumps(schema))
        rendered_msg = exc_info.value.messages[0]
        assert "format" in rendered_msg
        assert "multipleOf" in rendered_msg

    def test_supported_keys_accepted(self):
        """A schema using only supported keys passes validation."""
        schema = {
            "type": "object",
            "properties": {
                "temp": {
                    "type": "number",
                    "description": "Temperature",
                    "minimum": 0,
                    "maximum": 100,
                    "units": "°C",
                    "default": 20,
                },
            },
        }
        # Should not raise
        result = parse_json_schema_input(json.dumps(schema))
        assert result["properties"]["temp"]["type"] == "number"


# ── Authoritative JSON validation at launch ─────────────────────────────
#
# These tests exercise _validate_json_against_schema, the static method
# that enforces the workflow's input contract for paste/upload JSON
# submissions.  The method must reject malformed JSON and non-object
# payloads rather than silently passing them through.


class TestValidateJsonAgainstSchema:
    """Tests for WorkflowLaunchDetailView._validate_json_against_schema.

    This method is the authoritative contract gate for all non-form-mode
    JSON submissions.  It must never return an empty error list for
    input that violates the schema — doing so would let the submission
    bypass the contract the workflow author declared.
    """

    @pytest.fixture
    def simple_schema(self):
        """A minimal schema requiring a single string field."""
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        }

    def test_valid_json_passes(self, simple_schema):
        """Valid JSON conforming to the schema returns no errors."""
        errors = WorkflowLaunchDetailView._validate_json_against_schema(
            '{"name": "Alice"}',
            simple_schema,
        )
        assert errors == []

    def test_malformed_json_returns_error(self, simple_schema):
        """Malformed JSON text must produce a validation error, not pass silently.

        Previously this returned [] (no errors), allowing malformed
        JSON to reach the launch pipeline without contract enforcement.
        """
        errors = WorkflowLaunchDetailView._validate_json_against_schema(
            "not-json-at-all",
            simple_schema,
        )
        assert len(errors) == 1
        assert "Invalid JSON" in errors[0]

    def test_json_array_returns_error(self, simple_schema):
        """A JSON array is valid JSON but not a valid schema object."""
        errors = WorkflowLaunchDetailView._validate_json_against_schema(
            "[1, 2, 3]",
            simple_schema,
        )
        assert len(errors) == 1
        assert "object" in errors[0].lower()

    def test_json_string_returns_error(self, simple_schema):
        """A JSON string literal is not a valid schema object."""
        errors = WorkflowLaunchDetailView._validate_json_against_schema(
            '"just a string"',
            simple_schema,
        )
        assert len(errors) == 1
        assert "object" in errors[0].lower()

    def test_missing_required_field_returns_error(self, simple_schema):
        """A valid JSON object missing required fields is rejected."""
        errors = WorkflowLaunchDetailView._validate_json_against_schema(
            "{}",
            simple_schema,
        )
        assert len(errors) >= 1
        assert "name" in errors[0].lower()

    def test_type_error_returns_error(self, simple_schema):
        """Non-string payload (e.g. None) returns an error."""
        errors = WorkflowLaunchDetailView._validate_json_against_schema(
            None,
            simple_schema,
        )
        assert len(errors) == 1

    def test_empty_string_returns_error(self, simple_schema):
        """An empty string is malformed JSON and should be rejected."""
        errors = WorkflowLaunchDetailView._validate_json_against_schema(
            "",
            simple_schema,
        )
        assert len(errors) == 1


# ── Model-level schema validation ─────────────────────────────────────
#
# Workflow.clean() must enforce input_schema invariants so that direct
# saves outside WorkflowForm cannot persist contracts the runtime
# adapters cannot honour.


@pytest.mark.django_db
class TestWorkflowModelSchemaValidation:
    """Tests for Workflow.clean() enforcement of input_schema invariants.

    Without model-level validation, code that saves a Workflow directly
    (admin, management commands, API) could persist a schema with
    unsupported types or keywords.  The runtime adapters (Pydantic model
    builder, Django form builder) would then silently ignore those
    constraints, weakening the contract.
    """

    def test_valid_schema_accepted(self):
        """A well-formed schema conforming to the v1 subset passes clean()."""
        from validibot.workflows.tests.factories import WorkflowFactory

        wf = WorkflowFactory.build(
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "integer", "minimum": 0},
                },
                "required": ["value"],
            },
        )
        # Should not raise
        wf.clean()

    def test_unsupported_type_rejected(self):
        """A schema with an unsupported type is rejected at the model layer."""
        from validibot.workflows.tests.factories import WorkflowFactory

        wf = WorkflowFactory.build(
            input_schema={
                "type": "object",
                "properties": {
                    "data": {"type": "array"},
                },
            },
        )
        with pytest.raises(ValidationError) as exc_info:
            wf.clean()
        assert "input_schema" in exc_info.value.message_dict

    def test_unsupported_keywords_rejected(self):
        """A schema with unsupported property keywords is rejected."""
        from validibot.workflows.tests.factories import WorkflowFactory

        wf = WorkflowFactory.build(
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z]+$"},
                },
            },
        )
        with pytest.raises(ValidationError) as exc_info:
            wf.clean()
        assert "input_schema" in exc_info.value.message_dict

    def test_null_schema_accepted(self):
        """A workflow with no input_schema (None) passes clean()."""
        from validibot.workflows.tests.factories import WorkflowFactory

        wf = WorkflowFactory.build(input_schema=None)
        # Should not raise
        wf.clean()

    def test_empty_schema_accepted(self):
        """An empty dict {} is not a valid schema (no type key) and is
        rejected, but an empty-but-typed schema is fine.
        """
        from validibot.workflows.tests.factories import WorkflowFactory

        wf = WorkflowFactory.build(
            input_schema={"type": "object", "properties": {}},
        )
        # Should not raise — empty properties is valid
        wf.clean()

    def test_non_json_only_workflow_with_schema_rejected(self):
        """Input contracts require JSON-only workflows.

        A workflow that accepts XML and JSON should not be allowed to
        persist an input_schema — the contract only makes sense for
        JSON-only workflows.  Without this invariant, a direct save()
        could bypass the form-level check.
        """
        from validibot.workflows.tests.factories import WorkflowFactory

        wf = WorkflowFactory.build(
            allowed_file_types=[
                SubmissionFileType.JSON,
                SubmissionFileType.XML,
            ],
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
        )
        with pytest.raises(ValidationError) as exc_info:
            wf.clean()
        assert "input_schema" in exc_info.value.message_dict

    def test_json_only_workflow_with_schema_accepted(self):
        """A JSON-only workflow with a valid input_schema passes clean()."""
        from validibot.workflows.tests.factories import WorkflowFactory

        wf = WorkflowFactory.build(
            allowed_file_types=[SubmissionFileType.JSON],
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )
        # Should not raise
        wf.clean()

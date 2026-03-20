"""Build runtime validators from a workflow's stored input schema.

This module converts the canonical JSON Schema stored on ``Workflow.input_schema``
into:

- A **Pydantic model** (``build_pydantic_model``) for authoritative server-side
  validation of submitted data.
- An **eligibility check** (``workflow_has_input_form``) that determines whether
  a workflow qualifies for the structured-form launch experience.

Important: the stored JSON Schema is the canonical contract. The Pydantic model
produced here is a *derived runtime validator* for the supported v1 schema subset;
it does not replace or supersede the stored schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import create_model

from validibot.submissions.constants import SubmissionFileType

if TYPE_CHECKING:
    from validibot.workflows.models import Workflow

# ── Type mapping ─────────────────────────────────────────────────────────

JSONSCHEMA_TYPE_MAP: dict[str, type] = {
    "number": float,
    "integer": int,
    "string": str,
    "boolean": bool,
}

# JSON Schema keyword → Pydantic Field keyword translation for numeric
# constraints.  JSON Schema uses "minimum" / "exclusiveMinimum" whereas
# Pydantic uses "ge" / "gt".
_NUMERIC_CONSTRAINT_MAP: dict[str, str] = {
    "minimum": "ge",
    "maximum": "le",
    "exclusiveMinimum": "gt",
    "exclusiveMaximum": "lt",
}


def workflow_has_input_form(workflow: Workflow) -> bool:
    """Check whether a workflow is eligible for form-based submission.

    Both conditions must hold:
    1. The workflow's ``allowed_file_types`` is exactly ``["json"]``.
    2. The workflow's ``input_schema`` is non-null and has at least one property.
    """
    if set(workflow.allowed_file_types or []) != {SubmissionFileType.JSON}:
        return False
    schema = workflow.input_schema
    return bool(schema and schema.get("properties"))


def build_pydantic_model(
    schema: dict,
    model_name: str = "DynamicInput",
) -> type[BaseModel]:
    """Construct a Pydantic model from a stored JSON Schema.

    Handles flat properties only — no nested objects or arrays.

    Important: the stored JSON Schema remains the canonical contract.
    This model is a derived runtime validator for the supported schema subset.
    """
    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))
    fields: dict = {}

    for name, prop in properties.items():
        json_type = prop.get("type", "string")
        python_type: type = JSONSCHEMA_TYPE_MAP.get(json_type, str)
        is_required = name in required_fields
        field_kwargs: dict = {}

        if prop.get("description"):
            field_kwargs["description"] = prop["description"]

        # Units are flattened onto the property by Pydantic 2's
        # model_json_schema() — store them in json_schema_extra so a
        # round-trip through model_json_schema() preserves them.
        if prop.get("units"):
            field_kwargs["json_schema_extra"] = {"units": prop["units"]}

        # Numeric constraints
        if json_type in ("number", "integer"):
            for json_key in (
                "ge",
                "gt",
                "le",
                "lt",
                "minimum",
                "maximum",
                "exclusiveMinimum",
                "exclusiveMaximum",
            ):
                if json_key in prop:
                    pydantic_key = _NUMERIC_CONSTRAINT_MAP.get(json_key, json_key)
                    field_kwargs[pydantic_key] = prop[json_key]

        # Enum constraint → Literal type
        if "enum" in prop:
            python_type = Literal[tuple(prop["enum"])]  # type: ignore[valid-type]

        # Default value / optional handling
        if "default" in prop:
            field_kwargs["default"] = prop["default"]
        elif not is_required:
            field_kwargs["default"] = None
            python_type = python_type | None  # type: ignore[assignment]

        fields[name] = (python_type, Field(**field_kwargs))

    return create_model(model_name, **fields)

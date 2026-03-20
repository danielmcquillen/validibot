"""Build Django forms and display rows from a workflow's stored input schema.

This module converts the canonical JSON Schema stored on ``Workflow.input_schema``
into:

- A **Django Form class** (``schema_to_django_form``) rendered with Bootstrap 5 +
  crispy-forms on the launch page.
- **Requirement rows** (``schema_to_requirement_rows``) for the human-readable
  schema summary modal, the launch-page "Input requirements" tab, and the
  public workflow info page.

These are presentation adapters.  The authoritative validation is handled by the
Pydantic model built in ``schema_builder.py``.
"""

from __future__ import annotations

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Field as CrispyField
from crispy_forms.layout import Layout
from django import forms as django_forms

# ── JSON Schema type → Django field class ────────────────────────────────

FIELD_MAP: dict[str, type[django_forms.Field]] = {
    "number": django_forms.FloatField,
    "integer": django_forms.IntegerField,
    "string": django_forms.CharField,
    "boolean": django_forms.BooleanField,
}

# Human-friendly labels for JSON Schema types shown in the requirements table.
_TYPE_LABELS: dict[str, str] = {
    "number": "Number",
    "integer": "Integer",
    "string": "Text",
    "boolean": "Yes / No",
}


def schema_to_django_form(schema: dict) -> type[django_forms.Form]:
    """Convert a JSON Schema to a Django Form class.

    Handles: FloatField, IntegerField, CharField, BooleanField, TypedChoiceField.
    Does NOT handle: nested objects, arrays, conditional fields.

    Django form quirks addressed here:

    - **Enum coercion:** ``ChoiceField.clean()`` always returns strings.  For
      integer enums like ``building_class: [2, 3, 5, ...]`` this produces ``'2'``
      which Pydantic's ``Literal[2, 3, ...]`` rejects.  We use
      ``TypedChoiceField`` with ``coerce=int`` (or ``float`` for number enums).
    - **Numeric constraints at construction time:** Django installs
      ``MinValueValidator`` / ``MaxValueValidator`` only during ``__init__``.
      Setting ``min_value`` / ``max_value`` after construction does nothing.
    - **Exclusive bounds:** Django only supports inclusive bounds.  For integer
      types we convert (``exclusiveMinimum: 0`` → ``min_value: 1``).  For float
      types we use the exclusive value as the inclusive bound — the Pydantic
      layer enforces the strict bound.
    """
    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))
    form_fields: dict[str, django_forms.Field] = {}

    for name, prop in properties.items():
        json_type = prop.get("type", "string")
        required = name in required_fields

        # Build help_text from description + units
        description = prop.get("description", "")
        units = prop.get("units", "")
        help_text = (
            f"{description} ({units})"
            if description and units
            else description or units
        )

        # Enumerated values → TypedChoiceField
        if "enum" in prop:
            coerce_fn = {"integer": int, "number": float}.get(json_type, str)
            choices = [(v, str(v)) for v in prop["enum"]]
            field = django_forms.TypedChoiceField(
                choices=choices,
                coerce=coerce_fn,
                required=required,
                help_text=help_text,
            )
        # Numeric fields
        elif json_type in ("number", "integer"):
            field_class = FIELD_MAP[json_type]
            kwargs: dict = {"required": required, "help_text": help_text}

            # Exclusive minimum
            if "exclusiveMinimum" in prop:
                if json_type == "integer":
                    kwargs["min_value"] = prop["exclusiveMinimum"] + 1
                else:
                    kwargs["min_value"] = prop["exclusiveMinimum"]
            elif "minimum" in prop:
                kwargs["min_value"] = prop["minimum"]

            # Exclusive maximum
            if "exclusiveMaximum" in prop:
                if json_type == "integer":
                    kwargs["max_value"] = prop["exclusiveMaximum"] - 1
                else:
                    kwargs["max_value"] = prop["exclusiveMaximum"]
            elif "maximum" in prop:
                kwargs["max_value"] = prop["maximum"]

            field = field_class(**kwargs)
        # Other types (string, boolean)
        else:
            field_class = FIELD_MAP.get(json_type, django_forms.CharField)
            field = field_class(required=required, help_text=help_text)

        if "default" in prop:
            field.initial = prop["default"]

        form_fields[name] = field

    dynamic_form = type("DynamicSubmissionForm", (django_forms.Form,), form_fields)

    original_init = dynamic_form.__init__

    def __init__(self, *args, **kwargs):  # noqa: N807
        original_init(self, *args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.helper.layout = Layout(*(CrispyField(name) for name in self.fields))

    dynamic_form.__init__ = __init__

    return dynamic_form


# ── Human-readable schema summary ───────────────────────────────────────


def schema_to_requirement_rows(schema: dict) -> list[dict]:
    """Convert canonical JSON Schema properties into display rows.

    Each row is a dict suitable for rendering in a requirements table:

    - ``name``: field key
    - ``label``: human-readable description or field key
    - ``type_label``: friendly type string (e.g. "Number", "Integer")
    - ``required``: bool
    - ``enum_values``: list of allowed values, or None
    - ``constraints``: human-readable constraint string (e.g. "1 ≤ value ≤ 8")
    - ``default``: default value, or None
    - ``units``: unit string, or empty
    """
    properties = schema.get("properties", {})
    required_fields = set(schema.get("required", []))
    rows: list[dict] = []

    for name, prop in properties.items():
        json_type = prop.get("type", "string")
        constraints = _build_constraint_string(prop, json_type)

        rows.append(
            {
                "name": name,
                "label": prop.get("description") or name,
                "type_label": _TYPE_LABELS.get(json_type, json_type),
                "required": name in required_fields,
                "enum_values": prop.get("enum"),
                "constraints": constraints,
                "default": prop.get("default"),
                "units": prop.get("units", ""),
            },
        )

    return rows


def _build_constraint_string(prop: dict, json_type: str) -> str:
    """Build a human-readable constraint string from JSON Schema bounds."""
    if json_type not in ("number", "integer"):
        return ""

    parts: list[str] = []

    # Lower bound
    if "exclusiveMinimum" in prop:
        parts.append(f"> {prop['exclusiveMinimum']}")
    elif "minimum" in prop:
        parts.append(f"≥ {prop['minimum']}")

    # Upper bound
    if "exclusiveMaximum" in prop:
        parts.append(f"< {prop['exclusiveMaximum']}")
    elif "maximum" in prop:
        parts.append(f"≤ {prop['maximum']}")

    return ", ".join(parts)

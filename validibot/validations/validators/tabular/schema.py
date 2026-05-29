"""The Tabular Validator's internal schema model and its Table Schema parser.

The structured-config lane adopts the
[Frictionless Table Schema](https://specs.frictionlessdata.io/table-schema/)
field-and-constraints *vocabulary* — but Validibot is not a conformant Table
Schema processor and does not depend on the ``frictionless`` library. Instead a
descriptor (a plain ``dict``) is parsed into the small internal model here,
which native validation consumes. We follow the standard as far as is practical
and no further (ADR-2026-05-26, "Standards alignment").

Supported field types in V1 are the common scalars: ``string``, ``number``,
``integer``, ``boolean``, ``date``, ``datetime``. Exotic types (``geopoint``,
``geojson``, ``yearmonth``, …) are out of scope; an unrecognised type is treated
as ``string`` so an imported descriptor still loads rather than erroring.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field

# The field types V1 understands. Anything else is treated as ``string``.
SUPPORTED_TYPES: frozenset[str] = frozenset(
    {"string", "number", "integer", "boolean", "date", "datetime"},
)

_DEFAULT_TYPE = "string"


@dataclass(frozen=True)
class FieldConstraints:
    """Per-field constraints, mirroring Table Schema's ``constraints`` object.

    All are optional. ``required`` means the value must be present (non-null);
    ``unique`` means values must not repeat (nulls exempt, SQL-style). The
    numeric/length/pattern/enum constraints apply only to non-null, validly
    typed cells.
    """

    required: bool = False
    unique: bool = False
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    enum: tuple[str, ...] | None = None


@dataclass(frozen=True)
class FieldSpec:
    """A single declared column: its name, type, and constraints."""

    name: str
    type: str = _DEFAULT_TYPE
    constraints: FieldConstraints = dataclass_field(default_factory=FieldConstraints)


@dataclass(frozen=True)
class TabularSchema:
    """A parsed Table Schema: ordered fields plus an optional primary key.

    ``primary_key`` is the list of column names forming the key (one name for a
    simple key, several for a composite key). It is validated for uniqueness
    *and* non-nullness natively; ``unique`` field constraints are checked
    separately (and exempt nulls).
    """

    fields: tuple[FieldSpec, ...]
    primary_key: tuple[str, ...] = ()

    def field_names(self) -> list[str]:
        """Return the declared column names in declaration order."""
        return [f.name for f in self.fields]


def _coerce_optional_number(value: object) -> float | None:
    """Read a numeric constraint (``minimum``/``maximum``) as a float or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_optional_int(value: object) -> int | None:
    """Read an integer constraint (``minLength``/``maxLength``) as an int or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _parse_constraints(raw: object) -> FieldConstraints:
    """Parse a Table Schema ``constraints`` object into ``FieldConstraints``.

    Unknown keys are ignored (we understand the vocabulary as far as practical).
    ``enum`` values are normalised to strings, because cells are compared as the
    raw string read from the file.
    """
    if not isinstance(raw, dict):
        return FieldConstraints()

    enum_raw = raw.get("enum")
    enum: tuple[str, ...] | None = None
    if isinstance(enum_raw, (list, tuple)):
        enum = tuple(str(value) for value in enum_raw)

    pattern = raw.get("pattern")
    return FieldConstraints(
        required=bool(raw.get("required", False)),
        unique=bool(raw.get("unique", False)),
        minimum=_coerce_optional_number(raw.get("minimum")),
        maximum=_coerce_optional_number(raw.get("maximum")),
        min_length=_coerce_optional_int(raw.get("minLength")),
        max_length=_coerce_optional_int(raw.get("maxLength")),
        pattern=pattern if isinstance(pattern, str) else None,
        enum=enum,
    )


def _parse_primary_key(raw: object) -> tuple[str, ...]:
    """Parse Table Schema ``primaryKey`` (a string or a list of strings)."""
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(name) for name in raw)
    return ()


def parse_table_schema(descriptor: dict) -> TabularSchema:
    """Parse a Frictionless Table Schema descriptor into a ``TabularSchema``.

    Reads ``fields`` (each with ``name``, optional ``type``, optional
    ``constraints``) and ``primaryKey``. An unrecognised ``type`` falls back to
    ``string`` so a descriptor using an exotic type still loads. A field with
    no ``name`` is skipped — a nameless column cannot be addressed.

    Raises ``TypeError`` if ``descriptor`` is not a dict or its ``fields`` is
    not an array (wrong *type*), and ``ValueError`` if it has no usable fields
    (right type, unusable *value*). Either way a malformed schema fails loudly
    at configuration time rather than silently validating nothing.
    """
    if not isinstance(descriptor, dict):
        msg = "Table Schema descriptor must be a JSON object."
        raise TypeError(msg)

    raw_fields = descriptor.get("fields")
    if not isinstance(raw_fields, (list, tuple)):
        msg = "Table Schema descriptor must contain a 'fields' array."
        raise TypeError(msg)

    fields: list[FieldSpec] = []
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            continue
        name = raw_field.get("name")
        if not isinstance(name, str) or not name:
            continue
        declared_type = raw_field.get("type", _DEFAULT_TYPE)
        field_type = (
            declared_type
            if isinstance(declared_type, str) and declared_type in SUPPORTED_TYPES
            else _DEFAULT_TYPE
        )
        fields.append(
            FieldSpec(
                name=name,
                type=field_type,
                constraints=_parse_constraints(raw_field.get("constraints")),
            ),
        )

    if not fields:
        msg = "Table Schema descriptor has no usable fields."
        raise ValueError(msg)

    return TabularSchema(
        fields=tuple(fields),
        primary_key=_parse_primary_key(descriptor.get("primaryKey")),
    )

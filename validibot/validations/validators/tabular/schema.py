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

from django.utils.html import format_html
from django.utils.html import format_html_join
from django.utils.translation import gettext_lazy as _

# The field types V1 understands. Anything else is treated as ``string``.
SUPPORTED_TYPES: frozenset[str] = frozenset(
    {"string", "number", "integer", "boolean", "date", "datetime"},
)

_DEFAULT_TYPE = "string"
_SUPPORTED_CONSTRAINTS = frozenset(
    {
        "required",
        "unique",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "enum",
    },
)
_IGNORED_LOCALE_KEYS = frozenset({"decimalChar", "groupChar", "bareNumber"})


@dataclass(frozen=True)
class CompatibilityItem:
    """One schema-derived token named in a compatibility notice.

    ``name`` is the literal field name or constraint keyword copied from the
    descriptor; the HTML headline renders it inside a ``<code>`` span so authors
    can see exactly which schema entries a notice refers to. ``note`` is an
    optional plain qualifier shown *after* the code span — e.g. the unsupported
    type in "order_year (year)" — and is not itself code-styled.
    """

    name: str
    note: str = ""

    def __str__(self) -> str:
        return f"{self.name} ({self.note})" if self.note else self.name


@dataclass(frozen=True)
class SchemaCompatibilityNotice:
    """One author-facing warning about a descriptor feature V1 cannot enforce.

    ``message`` is the plain-text headline (names the feature and the fields it
    came from) used by non-HTML consumers — post-save toasts, logs, and tests.
    ``detail`` explains *why* it is shown — what the keyword does, why V1 cannot
    act on it, and what that means for validation — so an author who did not
    write the schema can understand each item.

    ``lead`` and ``items`` are the structured form of the headline: ``lead`` is
    the text before the list (no colon), and ``items`` are the schema tokens it
    names. They let :meth:`headline_html` rewrap each token in ``<code>`` for
    the review screen, while ``message`` keeps a flat string for everywhere else.
    """

    code: str
    message: str
    detail: str = ""
    lead: str = ""
    items: tuple[CompatibilityItem, ...] = ()

    def headline_html(self) -> str:
        """Return the headline with each schema token wrapped in ``<code>``.

        The result is HTML-safe: every field name / keyword copied from the
        descriptor is escaped by ``format_html`` before it is wrapped, so a
        hostile name (``<script>``) renders inert. Notices without items (e.g.
        foreign keys) fall back to the plain ``message``.
        """
        if not self.items:
            return format_html("{}", self.message)
        rendered_items = format_html_join(
            ", ",
            "<code>{}</code>{}",
            (
                (
                    item.name,
                    format_html(" ({})", item.note) if item.note else "",
                )
                for item in self.items
            ),
        )
        return format_html("{}: {}.", self.lead, rendered_items)


def _compat_notice(
    *,
    code: str,
    lead: str,
    detail: str,
    items: tuple[CompatibilityItem, ...] = (),
) -> SchemaCompatibilityNotice:
    """Build a notice, deriving the flat ``message`` from ``lead`` + ``items``.

    Keeping the derivation in one place means the plain-text ``message`` (toasts,
    tests) and the structured ``lead``/``items`` (the ``<code>`` headline) can
    never drift apart. A notice with items reads "<lead>: a, b." and one without
    is just "<lead>.".
    """
    if items:
        joined = ", ".join(str(item) for item in items)
        message = f"{lead}: {joined}."
    else:
        message = f"{lead}."
    return SchemaCompatibilityNotice(
        code=code,
        message=message,
        detail=detail,
        lead=lead,
        items=items,
    )


def table_schema_compatibility_notices(
    descriptor: object,
) -> list[SchemaCompatibilityNotice]:
    """Describe imported Table Schema features outside the V1 contract.

    The descriptor is still accepted and its unknown metadata is preserved.
    These notices prevent that permissive round-trip behavior from implying
    that Validibot enforces every preserved keyword.
    """
    if not isinstance(descriptor, dict):
        return []

    notices: list[SchemaCompatibilityNotice] = []
    if descriptor.get("foreignKeys"):
        notices.append(
            _compat_notice(
                code="foreign_keys",
                lead=str(_("Foreign keys are not enforced")),
                detail=str(
                    _(
                        "Your schema declares a foreignKeys relationship. A foreign "
                        "key asserts that a column's values exist in another table, "
                        "but a Tabular step validates one uploaded file on its own "
                        "and cannot see other tables — so it cannot be checked. The "
                        "keys are kept in your saved schema for portability.",
                    ),
                ),
            ),
        )
    # NOTE: ``missingValues`` is *not* listed here — V1 now interprets it (the
    # declared tokens are coerced to null, the same as an empty cell). See
    # ``parse_table_schema`` / ``coerce_cell``.

    raw_fields = descriptor.get("fields")
    if not isinstance(raw_fields, (list, tuple)):
        return notices

    unsupported_types: list[CompatibilityItem] = []
    locale_fields: list[CompatibilityItem] = []
    format_fields: list[CompatibilityItem] = []
    unsupported_constraints: set[str] = set()
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            continue
        name = str(raw_field.get("name") or _("Unnamed field"))
        declared_type = raw_field.get("type", _DEFAULT_TYPE)
        if isinstance(declared_type, str) and declared_type not in SUPPORTED_TYPES:
            unsupported_types.append(CompatibilityItem(name=name, note=declared_type))
        if any(key in raw_field for key in _IGNORED_LOCALE_KEYS):
            locale_fields.append(CompatibilityItem(name=name))
        if raw_field.get("format") not in (None, ""):
            format_fields.append(CompatibilityItem(name=name))
        constraints = raw_field.get("constraints")
        if isinstance(constraints, dict):
            unsupported_constraints.update(
                str(key)
                for key in constraints
                if key not in _SUPPORTED_CONSTRAINTS and not str(key).startswith("x-")
            )

    if unsupported_types:
        notices.append(
            _compat_notice(
                code="unsupported_types",
                lead=str(_("Unsupported field types become Text")),
                items=tuple(unsupported_types),
                detail=str(
                    _(
                        "V1 supports Text, Integer, Number, Boolean, Date, and "
                        "Date-time. These fields declare a Frictionless type outside "
                        "that set, so they import as Text: their values are still "
                        "checked as text (required, length, pattern, enum), but not "
                        "as the original type — e.g. a 'year' is not range-checked as "
                        "a year.",
                    ),
                ),
            ),
        )
    if locale_fields:
        notices.append(
            _compat_notice(
                code="locale_options",
                lead=str(_("Locale number options are ignored")),
                items=tuple(locale_fields),
                detail=str(
                    _(
                        "These number fields set locale options (decimalChar, "
                        "groupChar, or bareNumber). V1 parses numbers one fixed, "
                        "locale-free way — '.' as the decimal point and no thousands "
                        "separators — so a file validates identically on every "
                        "machine. The options are kept but not applied, so a value "
                        "like '1.234,56' will not parse as a number.",
                    ),
                ),
            ),
        )
    if format_fields:
        notices.append(
            _compat_notice(
                code="field_formats",
                lead=str(_("Field formats are not enforced")),
                items=tuple(format_fields),
                detail=str(
                    _(
                        "These fields declare a Frictionless 'format' (such as "
                        "email, uuid, or a date pattern). The V1 editor does not "
                        "enforce format refinements — to require a shape, add a "
                        "regex Pattern on a Text column. Dates and date-times are "
                        "parsed as ISO-8601 regardless of any declared format. The "
                        "keyword is kept in your saved schema.",
                    ),
                ),
            ),
        )
    if unsupported_constraints:
        notices.append(
            _compat_notice(
                code="unsupported_constraints",
                lead=str(_("Unknown constraints are not enforced")),
                items=tuple(
                    CompatibilityItem(name=keyword)
                    for keyword in sorted(unsupported_constraints)
                ),
                detail=str(
                    _(
                        "V1 enforces these constraints: required, unique, "
                        "minimum/maximum, minLength/maxLength, pattern, and enum. "
                        "The listed keys are something else (a non-standard or newer "
                        "keyword), so they are kept in your saved schema but have no "
                        "effect on validation.",
                    ),
                ),
            ),
        )
    return notices


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
    required_when_present: str | None = None


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

    ``missing_values`` are the raw cell strings coercion treats as null, from
    Table Schema's ``missingValues`` (default ``("",)``). The empty string is
    always included — a blank cell is always missing — so authors can *add*
    sentinels like ``NA`` without the footgun of accidentally making empty
    cells count as present.
    """

    fields: tuple[FieldSpec, ...]
    primary_key: tuple[str, ...] = ()
    missing_values: tuple[str, ...] = ("",)

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
    required_when_present = raw.get("x-validibot-requiredWhenPresent")
    return FieldConstraints(
        required=bool(raw.get("required", False)),
        unique=bool(raw.get("unique", False)),
        minimum=_coerce_optional_number(raw.get("minimum")),
        maximum=_coerce_optional_number(raw.get("maximum")),
        min_length=_coerce_optional_int(raw.get("minLength")),
        max_length=_coerce_optional_int(raw.get("maxLength")),
        pattern=pattern if isinstance(pattern, str) else None,
        enum=enum,
        required_when_present=(
            required_when_present
            if isinstance(required_when_present, str) and required_when_present
            else None
        ),
    )


def _parse_primary_key(raw: object) -> tuple[str, ...]:
    """Parse Table Schema ``primaryKey`` (a string or a list of strings)."""
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(name) for name in raw)
    return ()


def _parse_missing_values(descriptor: dict) -> tuple[str, ...]:
    """Parse Table Schema ``missingValues`` into the null-token set.

    Defaults to ``("",)`` — Frictionless's default, where only a blank cell is
    missing. When the descriptor lists tokens they are normalised to strings and
    the empty string is *always* kept in the set: V1 treats a blank cell as
    missing regardless, so declaring ``NA``/``NULL`` extends the set rather than
    replacing it (avoiding the spec footgun where omitting ``""`` would make
    empty cells count as present). Order is preserved and duplicates dropped so
    the set is stable and deterministic.
    """
    raw = descriptor.get("missingValues")
    if not isinstance(raw, (list, tuple)):
        return ("",)
    tokens: list[str] = [""]
    for value in raw:
        token = str(value)
        if token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def _validate_field_names(names: list[str]) -> None:
    """Reject field-name sets that would make a column unaddressable.

    Mirrors the header validation in ``readers/csv.py::_canonical_header_names``
    so the two schema-acquisition paths (pasted descriptor vs. inferred header)
    enforce the *same* contract. This is load-bearing, not cosmetic: for a
    headerless file the field names become the dataframe's column labels, and a
    duplicate label makes ``frame[name]`` return a ``DataFrame`` instead of a
    ``Series`` — so ``native.py``'s ``frame[field.name].tolist()`` would raise
    ``AttributeError`` mid-validation. Catching it here turns a crash into a
    clean configuration error.

    Three rules, all raising ``ValueError`` (an unusable *value*, not a wrong
    *type*):

    - blank-after-trim → a column with no usable name can't be addressed;
    - exact duplicate → ``row.value`` / ``frame[name]`` would be ambiguous;
    - case-only collision → Table Schema treats names as not-case-sensitive for
      uniqueness, so ``Lat`` vs ``lat`` is a collision, not two columns.
    """
    blanks = [i + 1 for i, name in enumerate(names) if name.strip() == ""]
    if blanks:
        msg = f"Table Schema has blank field name(s) at position(s): {blanks}."
        raise ValueError(msg)

    # Track first occurrence by trimmed+casefolded key so an exact duplicate
    # (same text) is distinguishable from a case-only collision.
    seen: dict[str, str] = {}
    for name in names:
        key = name.strip().casefold()
        if key in seen:
            if seen[key] == name:
                msg = f"Table Schema has a duplicate field name: {name!r}."
                raise ValueError(msg)
            msg = (
                f"Table Schema has field names that collide ignoring case: "
                f"{seen[key]!r} and {name!r}."
            )
            raise ValueError(msg)
        seen[key] = name


def _validate_conditional_requiredness(fields: list[FieldSpec]) -> None:
    """Validate references used by the Validibot conditional extension."""
    names = {field.name for field in fields}
    for field in fields:
        trigger = field.constraints.required_when_present
        if not trigger:
            continue
        if trigger == field.name:
            msg = f"Field {field.name!r} cannot be conditionally required by itself."
            raise ValueError(msg)
        if trigger not in names:
            msg = (
                f"Field {field.name!r} has an unknown conditional-requiredness "
                f"trigger: {trigger!r}."
            )
            raise ValueError(msg)


def parse_table_schema(descriptor: dict) -> TabularSchema:
    """Parse a Frictionless Table Schema descriptor into a ``TabularSchema``.

    Reads ``fields`` (each with ``name``, optional ``type``, optional
    ``constraints``) and ``primaryKey``. An unrecognised ``type`` falls back to
    ``string`` so a descriptor using an exotic type still loads. A field with
    no ``name`` is skipped — a nameless column cannot be addressed.

    Raises ``TypeError`` if ``descriptor`` is not a dict or its ``fields`` is
    not an array (wrong *type*), and ``ValueError`` if it has no usable fields
    or its field names are unusable — blank-after-trim, duplicated, or
    case-colliding (right type, unusable *value*). Either way a malformed schema
    fails loudly at configuration time rather than silently validating nothing
    (or, for duplicate names on a headerless file, crashing at run time — see
    :func:`_validate_field_names`).
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
        # A missing or non-string name is genuinely nameless (can't be
        # addressed) and is skipped; a present-but-blank name is a declared
        # mistake and is rejected by _validate_field_names below.
        if not isinstance(name, str):
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

    # Names must be unique and addressable before we build the schema — see
    # the docstring for why a duplicate would otherwise crash native validation.
    _validate_field_names([f.name for f in fields])
    _validate_conditional_requiredness(fields)

    return TabularSchema(
        fields=tuple(fields),
        primary_key=_parse_primary_key(descriptor.get("primaryKey")),
        missing_values=_parse_missing_values(descriptor),
    )

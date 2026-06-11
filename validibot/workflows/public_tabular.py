"""Submitter-facing display model for a Tabular Validator step.

The public workflow info page (:class:`PublicWorkflowInfoView`) shows each step
so a prospective submitter understands what to send. For a Tabular Validator
step that means the *shape of the CSV*: which columns are expected, their data
types, which are required, the allowed values and bounds, and the file-level
dialect (delimiter / encoding / header row).

This module turns the persisted Table Schema descriptor (stored as JSON in the
step's ``Ruleset.rules`` text) plus the step's stored dialect config into a
small, template-friendly structure. It is intentionally read-only and fails
soft: a missing or malformed descriptor yields ``None`` so the public page
simply omits the accordion rather than erroring — a public, anonymously
reachable page must never 500 on bad author data.

The type vocabulary and constraint set mirror
:mod:`validibot.validations.validators.tabular.schema` (the same parser the
validator uses at run time), so what a submitter is shown here is exactly what
will be enforced when they upload.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from validibot.validations.validators.tabular.schema import FieldConstraints
from validibot.validations.validators.tabular.schema import parse_table_schema
from validibot.workflows.forms import TABULAR_TYPE_CHOICES

logger = logging.getLogger(__name__)

# Human labels for the validator's supported field types, reused from the
# editor's column form so the public page and the authoring UI never disagree
# about what (say) a ``string`` column is called ("Text").
_TYPE_LABELS: dict[str, object] = dict(TABULAR_TYPE_CHOICES)

# Mirrors the delimiter choices offered in the Tabular settings form. The empty
# string is the "let the reader sniff it" option, surfaced as "Auto-detect".
_DELIMITER_LABELS: dict[str, object] = {
    "": _("Auto-detect"),
    ",": _("Comma"),
    "\t": _("Tab"),
    ";": _("Semicolon"),
    "|": _("Pipe"),
}


@dataclass(frozen=True)
class TabularColumnDisplay:
    """One column as a submitter needs to understand it.

    ``constraints`` is a short bounds string (numeric range / string length);
    ``pattern`` and ``enum_values`` are kept separate so the template can render
    them as code / value chips rather than inline prose.
    """

    name: str
    type_label: str
    required: bool
    unique: bool
    is_primary_key: bool
    title: str = ""
    description: str = ""
    enum_values: tuple[str, ...] = ()
    constraints: str = ""
    pattern: str = ""
    required_when: str = ""


@dataclass(frozen=True)
class TabularDialectDisplay:
    """File-level CSV options the submitter's file must match."""

    delimiter_label: str
    encoding: str
    has_header: bool


@dataclass(frozen=True)
class TabularPublicDetails:
    """Everything the public info page shows for a single Tabular step."""

    columns: tuple[TabularColumnDisplay, ...]
    dialect: TabularDialectDisplay
    primary_key: tuple[str, ...]
    column_count: int
    required_column_count: int


def _format_number(value: float) -> str:
    """Render a numeric bound without a misleading ``.0`` on whole numbers.

    The parser coerces every numeric constraint to ``float``, so a descriptor
    that said ``"minimum": 0`` arrives here as ``0.0``. Showing "≥ 0" reads far
    better to a submitter than "≥ 0.0", while a genuine ``0.5`` is preserved.
    """
    if value == int(value):
        return str(int(value))
    return str(value)


def _constraint_summary(constraints: FieldConstraints) -> str:
    """Build a short bounds string (e.g. ``"≥ 0, ≤ 100"``) for one column.

    Covers the numeric range and string-length bounds only. ``pattern`` and
    ``enum`` are surfaced by the caller as their own display fields.
    """
    parts: list[str] = []
    if constraints.minimum is not None:
        parts.append(f"≥ {_format_number(constraints.minimum)}")
    if constraints.maximum is not None:
        parts.append(f"≤ {_format_number(constraints.maximum)}")
    if constraints.min_length is not None:
        parts.append(str(_("length ≥ %(n)s") % {"n": constraints.min_length}))
    if constraints.max_length is not None:
        parts.append(str(_("length ≤ %(n)s") % {"n": constraints.max_length}))
    return ", ".join(parts)


def _delimiter_label(config: dict, metadata: dict) -> str:
    """Resolve a friendly delimiter label across config/metadata storage.

    Newer steps carry a ready-made ``delimiter_label`` in their config; older or
    imported steps only have the raw ``delimiter`` (in config or ruleset
    metadata), which we map to the same labels the settings form uses.
    """
    label = config.get("delimiter_label")
    if label:
        return str(label)
    delimiter = config.get("delimiter")
    if delimiter is None:
        delimiter = metadata.get("delimiter", "")
    return str(_DELIMITER_LABELS.get(delimiter, delimiter or _("Auto-detect")))


def build_tabular_public_details(
    *,
    schema_text: str,
    config: dict | None = None,
    metadata: dict | None = None,
) -> TabularPublicDetails | None:
    """Build the submitter-facing detail model for a Tabular step.

    Args:
        schema_text: The Table Schema descriptor as JSON text (the step's
            ``Ruleset.rules``).
        config: The step's ``config`` dict (dialect labels and counts).
        metadata: The ruleset ``metadata`` dict, used as a dialect fallback for
            imported steps whose config predates the display fields.

    Returns:
        A populated :class:`TabularPublicDetails`, or ``None`` when the
        descriptor is absent or unparseable — in which case the public page
        omits the accordion rather than surfacing a half-built panel.
    """
    config = config or {}
    metadata = metadata or {}

    if not schema_text or not schema_text.strip():
        return None
    try:
        descriptor = json.loads(schema_text)
    except (TypeError, ValueError):
        logger.warning("Tabular public details: descriptor is not valid JSON.")
        return None
    try:
        schema = parse_table_schema(descriptor)
    except (TypeError, ValueError) as exc:
        # Malformed descriptors are an author-side problem; the public page
        # should degrade to "no detail" rather than error for every visitor.
        logger.warning("Tabular public details: unparseable Table Schema: %s", exc)
        return None

    # ``title``/``description`` live on the raw descriptor, not the parsed
    # model (the parser keeps only name/type/constraints), so look them up by
    # name to enrich the columns with author-written guidance.
    raw_by_name: dict[str, dict] = {
        raw["name"]: raw
        for raw in descriptor.get("fields", [])
        if isinstance(raw, dict) and isinstance(raw.get("name"), str)
    }

    primary_key = schema.primary_key
    columns: list[TabularColumnDisplay] = []
    for spec in schema.fields:
        raw = raw_by_name.get(spec.name, {})
        constraints = spec.constraints
        title = raw.get("title")
        description = raw.get("description")
        columns.append(
            TabularColumnDisplay(
                name=spec.name,
                type_label=str(_TYPE_LABELS.get(spec.type, spec.type)),
                required=constraints.required,
                unique=constraints.unique,
                is_primary_key=spec.name in primary_key,
                title=title if isinstance(title, str) else "",
                description=description if isinstance(description, str) else "",
                enum_values=constraints.enum or (),
                constraints=_constraint_summary(constraints),
                pattern=constraints.pattern or "",
                required_when=constraints.required_when_present or "",
            ),
        )

    dialect = TabularDialectDisplay(
        delimiter_label=_delimiter_label(config, metadata),
        encoding=str(config.get("encoding") or metadata.get("encoding") or "utf-8"),
        has_header=bool(config.get("has_header", metadata.get("has_header", True))),
    )
    return TabularPublicDetails(
        columns=tuple(columns),
        dialect=dialect,
        primary_key=primary_key,
        column_count=len(columns),
        required_column_count=sum(1 for col in columns if col.required),
    )

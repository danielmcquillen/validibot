from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML
from crispy_forms.layout import Column
from crispy_forms.layout import Layout
from crispy_forms.layout import Row
from django import forms
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.utils.safestring import mark_safe
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from validibot.projects.models import Project
from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.cel import CUSTOM_HELPER_NAMES
from validibot.validations.cel_columns import referenced_row_columns
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import Severity
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorRuleType
from validibot.validations.constants import get_resource_type_config
from validibot.validations.constants import get_resource_types_for_validator
from validibot.validations.models import StepIODefinition
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.validators.shacl.form_fields import ShaclConfigMixin
from validibot.validations.validators.shacl.form_fields import _max_asks_per_step

# Hard upper bound on CEL expression length, enforced before regex-based
# identifier extraction. CEL expressions in practice are short (a few
# hundred characters at most); 4096 is generous headroom while bounding
# worst-case time on the string-literal stripper below.
_MAX_CEL_EXPRESSION_LEN = 4096
_SHACL_TARGET_GRAPH_CHOICES = (
    ("data", _("Submitted RDF data graph")),
    ("results", _("SHACL results graph")),
    ("union", _("Data + results graph")),
)


def _strip_cel_string_literals(expression: str) -> str:
    """Remove all CEL string literals from ``expression``.

    Used as a preprocessing step before identifier extraction so that
    identifier-shaped tokens inside string literals (``"p.foo"``) are
    not mistaken for bare identifiers. Caller is responsible for
    bounding the input length — this function does not enforce
    ``_MAX_CEL_EXPRESSION_LEN`` on its own.
    """

    output: list[str] = []
    quote: str | None = None
    escaped = False

    for char in expression:
        if quote is None:
            if char in {"'", '"'}:
                quote = char
                escaped = False
            else:
                output.append(char)
            continue

        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            quote = None

    return "".join(output)


def _scan_signal_bracket_access_keys(expression: str) -> list[str]:
    """Return ``s["<key>"]`` / ``signal["<key>"]`` keys outside string literals.

    Used by ``RulesetAssertionForm._validate_cel_identifiers`` to catch
    the CEL bracket-access spelling of the ``s.<step_input>`` mental-
    model trap. The CEL spec says ``m.x`` and ``m["x"]`` are equivalent
    for maps with valid keys, so an author can express the same wrong
    reference through either spelling. The dot-access form is caught
    by the identifier-regex pass; this scan handles the bracket form.

    Why a hand-written scanner instead of a regex:

    1. **String-literal awareness.** A naive regex over the raw
       expression matches ``s["foo"]`` text inside an ordinary CEL
       string literal — e.g. ``p.note == 's["foo"]'`` is a perfectly
       valid expression comparing a string, no bracket access happens
       at runtime. The scanner skips past quoted spans so the bracket
       match only fires on real syntax.
    2. **Slug-friendly keys.** ``StepIODefinition.contract_key`` is a
       Django ``SlugField`` and allows hyphens (e.g. ``panel-area``).
       A regex constrained to identifier-shaped keys would let
       ``s["panel-area"]`` slip through; this scanner extracts the
       quoted contents verbatim and leaves slug membership checks to
       the caller.

    The scanner returns a list (preserving order, allowing duplicates)
    so the caller can report all occurrences. Caller is responsible
    for membership lookup against ``inputs_by_slug`` and for the
    workflow-signal collision exception.
    """
    keys: list[str] = []
    i = 0
    n = len(expression)
    # Longest namespace root first so we don't partial-match ``s`` when
    # the source is actually ``signal``.
    roots = ("signal", "s")

    while i < n:
        ch = expression[i]

        # ── CEL string literal — skip its contents ────────────────────
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if expression[i] == "\\" and i + 1 < n:
                    i += 2  # skip backslash + the escaped character
                    continue
                if expression[i] == quote:
                    i += 1
                    break
                i += 1
            continue

        # ── Look for ``s | signal`` as a top-level expression token ──
        # Two distinct boundary checks have to both pass:
        #
        # 1. **Identifier boundary** — the previous character must
        #    not be a word character, or this candidate is just the
        #    tail of a longer identifier (``some_s``, ``my_signal``).
        # 2. **Expression-position boundary** — the previous
        #    non-whitespace character must not be ``.``, or this
        #    candidate is a field access on some other expression
        #    (``p.s["panel_area"]``, ``payload.signal["x"]``).
        #    Those are payload member references, not the CEL signal
        #    namespace, so they must not trigger the misnamespaced-
        #    input guard.
        #
        # The identifier-boundary check uses the immediate previous
        # char (whitespace-sensitive: ``some_s`` is one token, but
        # ``some_s `` and the trailing ``s`` are separate tokens —
        # the latter case is still caught by the expression-position
        # check, because the prior non-whitespace would be ``some_s``
        # ending in a word char). The expression-position check uses
        # the previous non-whitespace char to handle CEL's tolerance
        # for whitespace around ``.`` (``p . s`` is equivalent to
        # ``p.s``).
        prev_is_word = i > 0 and (
            expression[i - 1].isalnum() or expression[i - 1] == "_"
        )
        if prev_is_word:
            i += 1
            continue

        # Walk back over whitespace to find the previous non-
        # whitespace char; if it's ``.``, this is member access and
        # the candidate isn't a top-level namespace reference.
        prev_nonspace = ""
        j = i - 1
        while j >= 0 and expression[j].isspace():
            j -= 1
        if j >= 0:
            prev_nonspace = expression[j]
        if prev_nonspace == ".":
            i += 1
            continue

        matched_root: str | None = None
        for root in roots:
            end = i + len(root)
            if expression[i:end] != root:
                continue
            # Reject when the candidate is just the prefix of a longer
            # identifier (``signal_foo``, ``some_s_bar``).
            if end < n and (expression[end].isalnum() or expression[end] == "_"):
                continue
            matched_root = root
            break

        if matched_root is None:
            i += 1
            continue

        # Try to parse ``[ "<key>" ]`` from the position after the root.
        after_root = i + len(matched_root)
        key, consumed_to = _parse_bracket_key(expression, after_root)
        if key is not None:
            keys.append(key)
            i = consumed_to
        else:
            # No bracket access followed — skip past the root only.
            i = after_root

    return keys


def _parse_bracket_key(
    expression: str,
    start: int,
) -> tuple[str | None, int]:
    r"""Parse ``[ "<key>" ]`` starting at ``start``.

    Returns ``(unescaped_key, end_index)`` if a well-formed bracket
    access follows, where ``end_index`` is the position just after the
    closing ``]``. Returns ``(None, start)`` when the syntax doesn't
    match. Whitespace is allowed between every pair of tokens.

    Honours common CEL backslash escapes by treating ``\X`` as a
    literal ``X`` (good enough for matching contract_keys, which are
    slug-shaped and never contain control sequences).
    """
    i = start
    n = len(expression)

    while i < n and expression[i].isspace():
        i += 1
    if i >= n or expression[i] != "[":
        return None, start
    i += 1

    while i < n and expression[i].isspace():
        i += 1
    if i >= n or expression[i] not in ("'", '"'):
        return None, start
    quote = expression[i]
    i += 1

    chars: list[str] = []
    closed = False
    while i < n:
        if expression[i] == "\\" and i + 1 < n:
            chars.append(expression[i + 1])
            i += 2
            continue
        if expression[i] == quote:
            i += 1
            closed = True
            break
        chars.append(expression[i])
        i += 1
    if not closed:
        return None, start

    while i < n and expression[i].isspace():
        i += 1
    if i >= n or expression[i] != "]":
        return None, start

    return "".join(chars), i + 1


class CelHelpLabelMixin:
    """Provide a helper to append the CEL help tooltip to field labels."""

    @staticmethod
    def _cel_help_markup() -> SafeString:
        # Template content is developer-controlled, so mark_safe is appropriate here.
        return mark_safe(render_to_string("shared/cel_help_tooltip.html").strip())  # noqa: S308

    def _append_cel_help_to_label(self, field_name: str = "cel_expression") -> None:
        field = self.fields.get(field_name)
        if not field:
            return
        # Use format_html to safely escape the label while allowing our HTML wrapper.
        # _cel_help_markup() returns SafeString so format_html won't escape it.
        field.label = format_html(
            "<div class='d-flex flex-row justify-content-between'>{}{}</div>",
            field.label,
            self._cel_help_markup(),
        )


class ShaclLibraryValidatorCreateForm(ShaclConfigMixin, forms.Form):
    """Form to create an org-owned SHACL validator in the library.

    Mirrors the existing Custom + FMU library-validator forms in shape:
    name + descriptions at the top, validator-specific config fields below.
    The SHACL-specific fields (shapes, ontologies,
    bundled standards, engine knobs) come from
    :class:`validibot.validations.validators.shacl.form_fields.ShaclConfigMixin`
    so they stay in sync with the workflow step config form.

    On save, the create view calls
    :func:`validibot.validations.utils.create_shacl_library_validator`,
    which atomically creates a ``Validator`` row with
    ``validation_type=SHACL``, ``is_system=False``, plus a default
    ``Ruleset`` carrying the uploaded shapes + metadata. Workflow steps
    later reference the validator by slug and inherit its default
    ruleset's shapes via the engine's library + step merge.

    See ADR-2026-05-18 ``SHACL Validator for RDF Graph Validation``,
    section "Library-level custom SHACL validators".
    """

    name = forms.CharField(
        label=_("Name"),
        max_length=120,
        help_text=_(
            "Descriptive name shown in the validator library and the step "
            "picker. Use something specific so colleagues can tell several "
            "SHACL validators apart (e.g. 'MeridianCx 223P + G36').",
        ),
    )
    short_description = forms.CharField(
        label=_("Short description"),
        max_length=255,
        required=False,
        help_text=_("Shown in lists and cards."),
    )
    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text=_("Supports Markdown. Plain text also works."),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Optional notes shown to other authors in your org."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.render_hidden_fields = True
        self.helper.layout = Layout(
            "name",
            "short_description",
            "description",
            HTML(
                "<hr><h6 class='text-uppercase text-muted mt-3 mb-3'>{}</h6>".format(
                    _("SHACL shapes (required)")
                )
            ),
            "shapes_files",
            "shapes_text",
            HTML(
                "<hr><h6 class='text-uppercase text-muted mt-3 mb-3'>{}</h6>".format(
                    _("Supplementary ontologies (optional)")
                )
            ),
            "ontology_files",
            "ontology_text",
            # Bundled-standards section hidden pending Phase 2 (Brick +
            # QUDT bundle content ships then). Re-add the section here
            # when the bundles ship.
            HTML(
                "<hr><h6 class='text-uppercase text-muted mt-3 mb-3'>{}</h6>".format(
                    _("Advanced options")
                )
            ),
            "inference_mode",
            "advanced_shacl",
            "submission_format",
            HTML("<hr>"),
            "notes",
        )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        shape_files = cleaned.get("shapes_files") or []
        shape_text = (cleaned.get("shapes_text") or "").strip()
        ontology_files = cleaned.get("ontology_files") or []
        ontology_text = (cleaned.get("ontology_text") or "").strip()

        # Library validator creation always requires shapes — unlike the
        # step config form, there's no "keep-existing" mode at create
        # time.
        if not (shape_files or shape_text):
            err = _(
                "Provide at least one SHACL shape — upload one or more "
                "files or paste shapes inline.",
            )
            self.add_error("shapes_files", err)
            self.add_error("shapes_text", err)

        self.shacl_enforce_size_caps(shape_files, "shapes_files")
        self.shacl_enforce_size_caps(ontology_files, "ontology_files")
        self.shacl_syntax_pre_flight_files(shape_files, "shapes_files")
        self.shacl_syntax_pre_flight_files(ontology_files, "ontology_files")
        if shape_text:
            self.shacl_syntax_pre_flight_text(shape_text, "shapes_text")
        if ontology_text:
            self.shacl_syntax_pre_flight_text(ontology_text, "ontology_text")

        return cleaned


class ShaclLibraryValidatorUpdateForm(ShaclLibraryValidatorCreateForm):
    """Edit form for an existing org-owned SHACL library validator.

    Identical surface to the create form, with one important
    semantic difference: leaving the shapes upload + paste areas blank
    is treated as "keep the existing shapes" rather than a validation
    error. This mirrors the JSON Schema step config form's keep-mode.
    """

    def clean(self) -> dict[str, Any]:
        # Skip the parent's "shapes required" check; on update the
        # author can leave everything blank to keep existing content.
        # Run only the size caps + syntax pre-flight.
        cleaned = forms.Form.clean(self)  # bypass create-form's clean()
        shape_files = cleaned.get("shapes_files") or []
        shape_text = (cleaned.get("shapes_text") or "").strip()
        ontology_files = cleaned.get("ontology_files") or []
        ontology_text = (cleaned.get("ontology_text") or "").strip()

        self.shacl_enforce_size_caps(shape_files, "shapes_files")
        self.shacl_enforce_size_caps(ontology_files, "ontology_files")
        self.shacl_syntax_pre_flight_files(shape_files, "shapes_files")
        self.shacl_syntax_pre_flight_files(ontology_files, "ontology_files")
        if shape_text:
            self.shacl_syntax_pre_flight_text(shape_text, "shapes_text")
        if ontology_text:
            self.shacl_syntax_pre_flight_text(ontology_text, "ontology_text")

        return cleaned


class CustomValidatorCreateForm(forms.Form):
    """Form used to capture metadata for a new custom validator."""

    name = forms.CharField(
        label=_("Name"),
        max_length=120,
    )
    short_description = forms.CharField(
        label=_("Short description"),
        max_length=255,
        required=False,
        help_text=_("Shown in lists and cards."),
    )
    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text=_("Supports Markdown. Plain text also works."),
    )
    # custom_type removed from form — hardcoded to SIMPLE for now.
    # May re-enable with more options later.
    allow_custom_assertion_targets = forms.BooleanField(
        label=_("Allow custom data paths in assertions"),
        required=False,
        help_text=_(
            "Allow assertions against data paths not declared as "
            "step inputs or step outputs."
        ),
    )
    supported_data_formats = forms.ChoiceField(
        label=_("Supported data format"),
        choices=[
            (SubmissionDataFormat.JSON, SubmissionDataFormat.JSON.label),
            (SubmissionDataFormat.YAML, SubmissionDataFormat.YAML.label),
        ],
        required=False,
        initial=SubmissionDataFormat.JSON,
        help_text=_("Pick the single data format this validator will parse."),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Optional notes shown to other authors in your org."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(Column("name", css_class="col-12")),
            "short_description",
            "description",
            Row(
                Column("allow_custom_assertion_targets", css_class="col-12 col-xl-6"),
                Column("supported_data_formats", css_class="col-12 col-xl-6"),
            ),
            "notes",
        )


class FMUValidatorCreateForm(forms.Form):
    """Upload form used to create an FMU validator backed by an FMU asset."""

    name = forms.CharField(
        label=_("Name"),
        max_length=120,
    )
    short_description = forms.CharField(
        label=_("Short description"),
        max_length=255,
        required=False,
        help_text=_("Shown in lists and cards."),
    )
    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
    )
    project = forms.ModelChoiceField(
        label=_("Project"),
        queryset=Project.objects.none(),
        required=False,
        help_text=_("Optional project scope for this validator."),
    )
    fmu_file = forms.FileField(
        label=_("FMU file"),
        help_text=_("Upload an FMU archive containing modelDescription.xml."),
    )

    def __init__(self, *args, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Project.objects.none()
        if org:
            qs = Project.objects.filter(org=org).order_by("name")
        self.fields["project"].queryset = qs
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("name", css_class="col-12 col-xl-6"),
                Column("project", css_class="col-12 col-xl-6"),
            ),
            "short_description",
            "description",
            "fmu_file",
        )

    def clean_fmu_file(self):
        uploaded = self.cleaned_data.get("fmu_file")
        if not uploaded:
            raise ValidationError(_("Upload an FMU archive."))
        if uploaded.size <= 0:
            raise ValidationError(_("Uploaded file is empty."))
        if not uploaded.name.lower().endswith(".fmu"):
            raise ValidationError(_("Expected a .fmu file."))
        return uploaded


class CustomValidatorUpdateForm(forms.Form):
    """Edit form for an existing custom validator."""

    name = forms.CharField(
        label=_("Name"),
        max_length=120,
    )
    short_description = forms.CharField(
        label=_("Short description"),
        max_length=255,
        required=False,
        help_text=_("Shown in lists and cards."),
    )
    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text=_("Supports Markdown. Plain text also works."),
    )
    allow_custom_assertion_targets = forms.BooleanField(
        label=_("Allow custom data paths in assertions"),
        required=False,
        help_text=_(
            "Allow assertions against data paths not declared as "
            "step inputs or step outputs."
        ),
    )
    supported_data_formats = forms.ChoiceField(
        label=_("Supported data format"),
        choices=[
            (SubmissionDataFormat.JSON, SubmissionDataFormat.JSON.label),
            (SubmissionDataFormat.YAML, SubmissionDataFormat.YAML.label),
        ],
        required=False,
        help_text=_("Pick the single data format this validator will parse."),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(Column("name", css_class="col-12")),
            "short_description",
            "description",
            Row(
                Column("allow_custom_assertion_targets", css_class="col-12 col-md-6"),
                Column("supported_data_formats", css_class="col-12 col-md-6"),
            ),
            "notes",
        )


CUSTOM_ASSERTION_TARGET_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_\-]*"
    r"(?:"
    r"(?:\.[A-Za-z_][A-Za-z0-9_\-]*)"
    r"|"
    r"(?:\[[0-9]+\])"
    r")*$",
)

TOLERANCE_MODE_CHOICES = (
    ("absolute", _("Absolute value")),
    ("percent", _("Percent of target")),
)

CEL_SIGNAL_FUNCTIONS = (
    "series",
    "signal",
    "value",
)

COLLECTION_OPERATOR_CHOICES = [
    (AssertionOperator.EQ, AssertionOperator.EQ.label),
    (AssertionOperator.NE, AssertionOperator.NE.label),
    (AssertionOperator.LT, AssertionOperator.LT.label),
    (AssertionOperator.LE, AssertionOperator.LE.label),
    (AssertionOperator.GT, AssertionOperator.GT.label),
    (AssertionOperator.GE, AssertionOperator.GE.label),
    (AssertionOperator.CONTAINS, AssertionOperator.CONTAINS.label),
    (AssertionOperator.STARTS_WITH, AssertionOperator.STARTS_WITH.label),
    (AssertionOperator.ENDS_WITH, AssertionOperator.ENDS_WITH.label),
    (AssertionOperator.MATCHES, AssertionOperator.MATCHES.label),
]


@dataclass(frozen=True)
class _LiteralValue:
    """Helper container for normalized literal values."""

    value: Any
    source: str


class RulesetAssertionForm(CelHelpLabelMixin, forms.Form):
    """Form for creating/updating catalog-backed assertions."""

    assertion_type = forms.ChoiceField(
        label=_("Assertion Type"),
        choices=AssertionType.choices,
    )
    target_data_path = forms.CharField(
        label=_("Target Path"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
            },
        ),
    )
    target_catalog_entry = forms.ChoiceField(
        label=_("Catalog Entry"),
        required=False,
        choices=[],
    )
    operator = forms.ChoiceField(
        label=_("Condition"),
        choices=[],
        required=False,
    )
    comparison_value = forms.CharField(
        label=_("Value"),
        required=False,
    )
    comparison_value_secondary = forms.CharField(
        label=_("Second value"),
        required=False,
    )
    list_values = forms.CharField(
        label=_("Values (one per line)"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
    )
    regex_pattern = forms.CharField(
        label=_("Regular expression"),
        required=False,
    )
    include_min = forms.BooleanField(
        label=_("Include minimum"),
        required=False,
        initial=True,
    )
    include_max = forms.BooleanField(
        label=_("Include maximum"),
        required=False,
        initial=True,
    )
    case_insensitive = forms.BooleanField(
        label=_("Case-insensitive"),
        required=False,
    )
    unicode_fold = forms.BooleanField(
        label=_("Unicode/locale fold"),
        required=False,
    )
    coerce_types = forms.BooleanField(
        label=_("Coerce numeric strings"),
        required=False,
    )
    treat_missing_as_null = forms.BooleanField(
        label=_("Treat missing as null"),
        required=False,
    )
    tolerance_value = forms.DecimalField(
        label=_("Tolerance"),
        required=False,
    )
    tolerance_mode = forms.ChoiceField(
        label=_("Tolerance type"),
        required=False,
        choices=TOLERANCE_MODE_CHOICES,
    )
    datetime_value = forms.DateTimeField(
        label=_("Date/Time"),
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    collection_operator = forms.ChoiceField(
        label=_("Collection member condition"),
        required=False,
        choices=COLLECTION_OPERATOR_CHOICES,
    )
    collection_value = forms.CharField(
        label=_("Collection member value"),
        required=False,
    )
    severity = forms.ChoiceField(
        label=_("Severity"),
        choices=Severity.choices,
        initial=Severity.ERROR,
    )
    when_expression = forms.CharField(
        label=_("When (optional)"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": _("CEL expression, e.g., has(series('fac_elec'))"),
            }
        ),
    )
    message_template = forms.CharField(
        label=_("Failure message"),
        required=False,
        help_text=_(
            "Shown when the assertion fails. "
            "Supports {{value}} style placeholders plus filters round, "
            "upper, lower, default."
        ),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Use template variables like {{value}}"),
            }
        ),
    )
    success_message = forms.CharField(
        label=_("Success message"),
        required=False,
        help_text=_(
            "Shown when the assertion passes. Supports {{value}} style placeholders."
        ),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("e.g. EUI is within acceptable range"),
            }
        ),
    )
    cel_expression = forms.CharField(
        label=_("CEL expression"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    shacl_description = forms.CharField(
        label=_("Description"),
        required=False,
        max_length=255,
        help_text=_("Short label shown on the assertion card and findings."),
    )
    shacl_target_graph = forms.ChoiceField(
        label=_("SHACL graph"),
        choices=_SHACL_TARGET_GRAPH_CHOICES,
        initial="data",
        required=False,
        help_text=_(
            "Run against the submitted data graph, the SHACL report graph, "
            "or a union of both.",
        ),
    )
    shacl_query = forms.CharField(
        label=_("SPARQL ASK query"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 8,
                "spellcheck": "false",
                "placeholder": "ASK { ?s ?p ?o }",
            },
        ),
        help_text=_(
            "Only SPARQL ASK is supported. SERVICE, FROM, and update "
            "operations are rejected.",
        ),
    )

    def __init__(
        self,
        *args,
        catalog_choices=None,
        catalog_entries=None,
        validator=None,
        target_slug_datalist_id=None,
        workflow_signal_names=None,
        shacl_sparql_assertion_count=0,
        tabular_columns=None,
        **kwargs,
    ):
        # Ignore fmu_variables kwarg if passed.
        kwargs.pop("fmu_variables", None)
        super().__init__(*args, **kwargs)
        self.shacl_query_max_length = self._resolve_shacl_query_max_length()
        self.catalog_choices = list(catalog_choices or [])
        # catalog_entries parameter contains StepIODefinition objects
        # (passed from the mixin's signal_definitions query).
        self.signal_definitions: list[StepIODefinition] = list(catalog_entries or [])
        # Workflow-level signal names (signal mappings + promoted outputs
        # from upstream steps).  These are valid s.* assertion targets
        # even when they don't correspond to a validator input.
        self.workflow_signal_names: set[str] = set(workflow_signal_names or [])
        self.inputs_by_slug: dict[str, StepIODefinition] = {}
        self.outputs_by_slug: dict[str, StepIODefinition] = {}
        self.choice_map: dict[str, StepIODefinition] = {}
        for sig in self.signal_definitions:
            if sig.direction == SignalDirection.OUTPUT:
                self.outputs_by_slug.setdefault(sig.contract_key, sig)
            else:
                self.inputs_by_slug.setdefault(sig.contract_key, sig)

        # Step-level FMU signals are now included in signal_definitions
        # via the mixin's get_catalog_choices(). Build name sets for
        # CEL validation and target resolution using native_name (the
        # original FMU variable name from modelDescription.xml).
        self.fmu_input_names: set[str] = set()
        self.fmu_output_names: set[str] = set()
        for sig in self.signal_definitions:
            if sig.origin_kind == SignalOriginKind.FMU:
                fmu_name = sig.native_name or sig.contract_key
                if sig.direction == SignalDirection.INPUT:
                    self.fmu_input_names.add(fmu_name)
                elif sig.direction == SignalDirection.OUTPUT:
                    self.fmu_output_names.add(fmu_name)

        self.catalog_slugs = set(
            list(self.inputs_by_slug.keys()) + list(self.outputs_by_slug.keys())
        )
        self.validator = validator
        # Declared column names for a Tabular Validator step (from the step's
        # stored Table Schema). Used to reject a row assertion that references
        # an undeclared column at save time — the ADR's column-existence
        # obligation. Empty when not a tabular step or no schema yet.
        self.tabular_columns: set[str] = set(tabular_columns or [])
        self.shacl_sparql_assertion_count = shacl_sparql_assertion_count
        self.target_slug_datalist_id = target_slug_datalist_id
        self._configure_shacl_query_field()
        signal_choices = []
        for sig in self.signal_definitions:
            role = (
                _("Output") if sig.direction == SignalDirection.OUTPUT else _("Input")
            )
            label = sig.label or sig.contract_key
            value = f"{sig.direction}:{sig.contract_key}"
            self.choice_map[value] = sig
            signal_choices.append((value, f"{label} · {role}"))
        self.no_signal_choices = len(signal_choices) == 0
        self.fields["target_catalog_entry"].choices = [
            ("", _("Select a step input or step output")),
            *signal_choices,
        ]
        # Hide catalog selector in favor of the target field; we
        # still keep it for backend resolution.
        self.fields["target_catalog_entry"].widget = forms.HiddenInput()
        if self.no_signal_choices:
            self.fields["target_catalog_entry"].required = False

        if self.fields.get("cel_expression"):
            if self._validator_allows_custom_targets():
                cel_help_text = _(
                    "You may enter new targets using dot notation "
                    "(e.g., data.error.message) "
                    "and [index] for lists."
                )
            else:
                cel_help_text = _(
                    "Only declared step inputs and step outputs "
                    "are available for this validator."
                )
            self.fields["cel_expression"].help_text = cel_help_text

        target_path_field = self.fields["target_data_path"]
        target_path_field.label = _("Target Path")
        base_help = _(
            "Use s.<name> for workflow signals, "
            "p.<path> for payload data, "
            "o.<name> for step outputs."
        )
        if self._validator_allows_custom_targets():
            target_path_field.help_text = (
                str(base_help)
                + " "
                + str(
                    _(
                        "You may also enter a custom dot-notation path "
                        "(e.g. data.results[0].value)."
                    )
                )
            )
        else:
            target_path_field.help_text = base_help
        target_attrs = target_path_field.widget.attrs
        target_attrs.update(
            {
                "placeholder": _("e.g. s.panel_area, p.building.area, o.site_eui"),
            },
        )
        if self.target_slug_datalist_id:
            target_attrs.update(
                {
                    "list": self.target_slug_datalist_id,
                },
            )
        operator_choices = [("", _("(Select one)"))]
        operator_choices.extend(self._basic_operator_choices())
        self.fields["operator"].choices = operator_choices
        self.fields["assertion_type"].choices = self._assertion_type_choices()
        if self._validator_is_shacl() and not self.initial.get("assertion_type"):
            self.fields["assertion_type"].initial = AssertionType.SHACL
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("assertion_type", css_class="col-12 col-lg-3"),
            ),
            "cel_expression",
            Row(
                Column("shacl_description", css_class="col-12 col-lg-4"),
                Column("shacl_target_graph", css_class="col-12 col-lg-4"),
            ),
            "shacl_query",
            Row(
                Column("target_data_path", css_class="col-12"),
            ),
            Row(
                Column("operator", css_class="col-12"),
            ),
            Row(
                Column("comparison_value", css_class="col-12 col-lg-6"),
                Column("comparison_value_secondary", css_class="col-12 col-lg-6"),
            ),
            "list_values",
            "regex_pattern",
            Row(
                Column("include_min", css_class="col-6 col-lg-3"),
                Column("include_max", css_class="col-6 col-lg-3"),
                Column("case_insensitive", css_class="col-6 col-lg-3"),
                Column("unicode_fold", css_class="col-6 col-lg-3"),
            ),
            Row(
                Column("coerce_types", css_class="col-6 col-lg-3"),
                Column("treat_missing_as_null", css_class="col-6 col-lg-3"),
                Column("tolerance_value", css_class="col-6 col-lg-3"),
                Column("tolerance_mode", css_class="col-6 col-lg-3"),
            ),
            Row(
                Column("datetime_value", css_class="col-12 col-lg-4"),
                Column("collection_operator", css_class="col-12 col-lg-4"),
                Column("collection_value", css_class="col-12 col-lg-4"),
            ),
            Row(
                Column("severity", css_class="col-12 col-lg-4"),
                Column("when_expression", css_class="col-12 col-lg-8"),
            ),
            "message_template",
            "success_message",
        )
        self._append_cel_help_to_label("cel_expression")

    def clean(self):
        cleaned = super().clean()
        assertion_type = cleaned.get("assertion_type")
        if assertion_type == AssertionType.CEL_EXPRESSION:
            # CEL expressions declare their own targets inside the expression.
            self.cleaned_data["resolved_signal"] = None
            self.cleaned_data["target_catalog_entry"] = None
            self.cleaned_data["target_data_path_value"] = ""
        elif assertion_type == AssertionType.SHACL:
            self._clean_shacl_assertion()
        else:
            self._resolve_target_data_path()
        if assertion_type == AssertionType.BASIC:
            operator_value = cleaned.get("operator")
            if not operator_value:
                raise ValidationError({"operator": _("Select a condition.")})
            # BASIC assertions targeting i.* / s.* / o.* used to be
            # rejected here because the BASIC evaluator walked the raw
            # payload root and had no way to reach values resolved
            # through StepInputBinding or workflow_signals. Phase 5 of
            # the May 2026 cleanup wired payload enrichment into the
            # validator base layer (see
            # ``BaseValidator._enrich_basic_payload``): the validator
            # now merges resolved bindings, workflow signals, and
            # output signals into the BASIC payload by their bare
            # contract_key before evaluation. With that wiring in
            # place, ``target_signal_definition.contract_key`` lookups
            # find the right value regardless of which namespace the
            # author chose. The form-side rejection is therefore
            # obsolete — kept as a brief comment for the next reader
            # who wonders why we trust BASIC + i.* now.
            rhs, options = self._build_basic_payload(AssertionOperator(operator_value))
            cleaned["rhs_payload"] = rhs
            cleaned["options_payload"] = options
            cleaned["resolved_operator"] = operator_value
            cleaned["cel_expression"] = ""
            cleaned["cel_cache"] = self._build_cel_preview(
                operator_value,
                rhs,
                options,
                cleaned.get("when_expression") or "",
            )
        elif assertion_type == AssertionType.CEL_EXPRESSION:
            expression = self._clean_cel_expression()
            # Always enforce namespace prefixes (p., s., output., steps.)
            # regardless of allow_custom_assertion_targets. The runtime
            # no longer promotes bare payload keys, so bare identifiers
            # would fail at evaluation time.
            self._validate_cel_identifiers(expression)
            cleaned["rhs_payload"] = {"expr": expression}
            # On a tabular step, tag the assertion's stage (row vs dataset) so
            # the validator buckets it correctly; empty for other validators.
            options_payload = self._tabular_cel_options(expression)
            cleaned["options_payload"] = options_payload
            # A row assertion may only reference columns declared in the step's
            # Table Schema — catch a typo'd/absent column here rather than at
            # run time (the ADR's column-existence obligation).
            if options_payload.get("tabular_stage") == "row":
                self._check_row_columns_exist(expression)
            cleaned["resolved_operator"] = AssertionOperator.CEL_EXPR
            cleaned["cel_cache"] = expression
            # Ensure the target constraint is satisfied for CEL assertions.
            cleaned["target_catalog_entry"] = None
            cleaned["target_data_path_value"] = expression or "__cel__"
        return cleaned

    def _assertion_type_choices(self):
        base = [
            (AssertionType.BASIC.value, AssertionType.BASIC.label),
            (
                AssertionType.CEL_EXPRESSION.value,
                AssertionType.CEL_EXPRESSION.label,
            ),
        ]
        if self._validator_is_shacl():
            return [(AssertionType.SHACL.value, AssertionType.SHACL.label), *base]
        return base

    def _validator_is_shacl(self) -> bool:
        return getattr(self.validator, "validation_type", None) == ValidationType.SHACL

    def _validator_is_tabular(self) -> bool:
        return (
            getattr(self.validator, "validation_type", None) == ValidationType.TABULAR
        )

    def _tabular_cel_options(self, expression: str) -> dict[str, str]:
        """Tag a CEL assertion on a tabular step with its stage.

        The Tabular Validator buckets assertions by ``options["tabular_stage"]``
        (ADR-2026-05-26's persistence decision): an expression that references
        ``row.*`` is a row-stage assertion (the validator's per-row loop owns
        it); anything else (``i.*``/``s.*``) is a dataset-stage assertion that
        flows through the generic input lane. Returns an empty dict for
        non-tabular steps so their options are unchanged.
        """
        if not self._validator_is_tabular():
            return {}
        stripped = _strip_cel_string_literals(expression)
        # ``row`` used as a namespace root (dot or bracket access), not as a
        # substring of another identifier like ``arrow``.
        if re.search(r"(?:^|[^\w.])row(?:\.|\[)", stripped):
            return {"tabular_stage": "row"}
        return {"tabular_stage": "dataset"}

    def _row_columns_referenced(self, expression: str) -> set[str]:
        """Return the column names a row assertion references via ``row.*``.

        Delegates to :func:`validibot.validations.cel_columns.referenced_row_columns`
        so the authoring form and the workflow importer share one definition of
        "which columns does this row assertion reference" — they must agree, or a
        row assertion that saves here could be rejected on import.
        """
        return referenced_row_columns(expression)

    def _check_row_columns_exist(self, expression: str) -> None:
        """Reject a row assertion that references an undeclared column.

        Skipped when the step has no declared columns yet (no schema
        configured) — there's nothing to check against, and the runtime still
        guards (an unbound ``row.x`` fails the run rather than passing). When
        the schema *is* configured, every referenced column must be declared,
        per the ADR's "to assert on a column you must declare it" rule.
        """
        if not self.tabular_columns:
            return
        referenced = self._row_columns_referenced(expression)
        unknown = sorted(referenced - self.tabular_columns)
        if unknown:
            self.add_error(
                "cel_expression",
                _(
                    "Row assertion references column(s) not declared in the "
                    "step's schema: %(unknown)s. Declared columns: %(declared)s."
                )
                % {
                    "unknown": ", ".join(unknown),
                    "declared": ", ".join(sorted(self.tabular_columns)),
                },
            )

    @staticmethod
    def _resolve_shacl_query_max_length() -> int:
        """Return the configured SPARQL ASK query length cap."""
        from validibot.validations.validators.shacl.sparql_security import (
            resolve_limits,
        )

        return resolve_limits().max_query_length

    def _configure_shacl_query_field(self) -> None:
        """Expose the SPARQL length cap in the textarea and field help."""
        field = self.fields["shacl_query"]
        field.widget.attrs["maxlength"] = str(self.shacl_query_max_length)
        field.help_text = _(
            "Only SPARQL ASK is supported. SERVICE, FROM, and update "
            "operations are rejected. Maximum length: %(limit)s characters.",
        ) % {"limit": f"{self.shacl_query_max_length:,}"}

    def _clean_shacl_assertion(self) -> None:
        """Validate and normalize a SHACL SPARQL ASK assertion row."""
        if not self._validator_is_shacl():
            raise ValidationError(
                {
                    "assertion_type": _(
                        "SHACL assertions are only available on SHACL validator steps.",
                    ),
                },
            )

        cap = _max_asks_per_step()
        if self.shacl_sparql_assertion_count >= cap:
            raise ValidationError(
                {
                    "assertion_type": _(
                        "This step already has %(cap)d SHACL SPARQL assertions. "
                        "Remove one before adding another.",
                    )
                    % {"cap": cap},
                },
            )

        target_graph = self.cleaned_data.get("shacl_target_graph") or "data"
        valid_targets = {choice[0] for choice in _SHACL_TARGET_GRAPH_CHOICES}
        if target_graph not in valid_targets:
            raise ValidationError(
                {"shacl_target_graph": _("Select a SHACL target graph.")},
            )

        query = (self.cleaned_data.get("shacl_query") or "").strip()
        if not query:
            raise ValidationError(
                {"shacl_query": _("Provide a SPARQL ASK query.")},
            )
        if len(query) > self.shacl_query_max_length:
            raise ValidationError(
                {
                    "shacl_query": _(
                        "SPARQL query exceeds the maximum length of "
                        "%(limit)s characters (got %(got)s).",
                    )
                    % {
                        "limit": f"{self.shacl_query_max_length:,}",
                        "got": f"{len(query):,}",
                    },
                },
            )

        try:
            from validibot.validations.validators.shacl.sparql_security import (
                SparqlScrubError,
            )
            from validibot.validations.validators.shacl.sparql_security import (
                scrub_sparql_ask,
            )

            scrub_sparql_ask(query)
        except SparqlScrubError as exc:
            raise ValidationError(
                {
                    "shacl_query": _(
                        "SPARQL query failed security scrub: %(err)s",
                    )
                    % {"err": exc},
                },
            ) from exc

        description = (self.cleaned_data.get("shacl_description") or "").strip()
        self.cleaned_data["resolved_signal"] = None
        self.cleaned_data["target_catalog_entry"] = None
        self.cleaned_data["target_data_path_value"] = f"shacl.{target_graph}"
        self.cleaned_data["resolved_stage"] = CatalogRunStage.OUTPUT
        self.cleaned_data["when_expression"] = ""
        self.cleaned_data["rhs_payload"] = {
            "target_graph": target_graph,
            "query": query,
            "description": description,
        }
        self.cleaned_data["options_payload"] = {}
        self.cleaned_data["resolved_operator"] = AssertionOperator.SPARQL_ASK
        self.cleaned_data["cel_expression"] = ""
        self.cleaned_data["cel_cache"] = query

    def _basic_operator_choices(self):
        return [
            (choice.value, choice.label)
            for choice in AssertionOperator
            if choice not in {AssertionOperator.CEL_EXPR, AssertionOperator.SPARQL_ASK}
        ]

    def _validate_cel_identifiers(self, expression: str) -> None:
        """Validate that CEL identifiers use the namespaced convention.

        All data references must use a namespace prefix:
        - ``p.key`` / ``payload.key`` — raw submission data
        - ``s.name`` / ``signal.name`` — author-defined signals
        - ``o.name`` / ``output.name`` — this step's step outputs
        - ``steps.key.output.name`` — upstream step outputs

        Bare identifiers are only allowed for CEL literals (``true``,
        ``false``, ``null``), CEL built-in functions, and single-letter
        variables used as loop vars in quantifier expressions.
        """
        reserved_literals = {"true", "false", "null"}
        # The namespace root names that are valid bare identifiers.
        # Five CEL namespaces per ADR-2026-05-22b: payload (p), signal (s),
        # input (i), output (o), steps.
        namespace_roots = {
            "p",
            "payload",
            "s",
            "signal",
            "i",
            "input",
            "o",
            "output",
            "steps",
        }
        # row.* is a step-local namespace bound only by the Tabular Validator's
        # row-stage loop, so it is accepted only on a tabular step (mandatory
        # scoping per ADR-2026-05-26). col.* is deferred with V2.
        if self._validator_is_tabular():
            namespace_roots.add("row")
        cel_builtins = {
            "has",
            "exists",
            "exists_one",
            "all",
            "map",
            "filter",
            "size",
            "contains",
            "startsWith",
            "endsWith",
            "type",
            "int",
            "double",
            "string",
            "bool",
            "abs",
            "ceil",
            "floor",
            "round",
            "timestamp",
            "duration",
            "matches",
            "in",
        }
        # Validibot-specific helper functions — sourced from the single
        # canonical set (DEFAULT_HELPERS via CUSTOM_HELPER_NAMES) so this
        # allowlist cannot drift from the documented/bound helper set.
        cel_builtins.update(CUSTOM_HELPER_NAMES)

        # Reject pathological lengths up-front. ``_strip_cel_string_literals``
        # is linear-time on inputs that pass this check; without the cap an
        # attacker could submit a multi-MB string and consume CPU even
        # though every legitimate expression is well under 4 KB.
        if len(expression) > _MAX_CEL_EXPRESSION_LEN:
            raise ValidationError(
                {
                    "cel_expression": _(
                        "Expression is too long "
                        "(%(length)d characters; max %(limit)d).",
                    )
                    % {
                        "length": len(expression),
                        "limit": _MAX_CEL_EXPRESSION_LEN,
                    },
                },
            )

        unknown = set()
        # ``s.<step_input>`` is a mental-model trap: the form happily
        # resolves the name (the s.* dispatcher checks ``inputs_by_slug``
        # before ``workflow_signal_names``), so the CEL identifier check
        # would also accept it as a "namespaced reference". But at
        # runtime step inputs live in ``i.*`` — they are NOT injected
        # into ``s.*``. The author's CEL expression saves, then silently
        # reads null at evaluation time. We collect these and surface
        # them as a separate, more specific error.
        misnamespaced_inputs: set[str] = set()

        # ── Pre-strip pass: bracket-access trap (``s["name"]``) ──────
        # Per the CEL spec, ``m.x`` and ``m["x"]`` are equivalent for
        # maps with valid keys. If we only scanned the stripped
        # expression, ``s["panel_area"]`` would have the string
        # literal removed first, leaving just ``s`` for the
        # identifier regex — that's a valid namespace root, so the
        # guard below wouldn't fire and the same mental-model trap
        # would slip through a different valid CEL spelling.
        #
        # We hand off to a small lexical scanner (rather than a
        # regex) because:
        #
        # 1. The scanner skips over CEL string literals, so the
        #    text ``s["foo"]`` inside an ordinary string (e.g.
        #    ``p.note == 's["foo"]'``) doesn't false-positive as
        #    actual bracket access.
        # 2. The scanner extracts the bracket key verbatim and
        #    leaves slug membership to us. ``contract_key`` is a
        #    Django SlugField (hyphens allowed), so an
        #    identifier-shaped regex would let ``s["panel-area"]``
        #    slip through.
        #
        # The collision exception is the same as the dot-access
        # branch: a key that's also a real workflow signal name is
        # legitimate (runtime resolves it to the workflow signal's
        # value, not to a step input).
        for bracket_key in _scan_signal_bracket_access_keys(expression):
            if (
                bracket_key in self.inputs_by_slug
                and bracket_key not in self.workflow_signal_names
            ):
                misnamespaced_inputs.add(bracket_key)

        # Strip string literals (including escaped quotes) so identifiers
        # inside quotes are not treated as bare identifiers.
        stripped = _strip_cel_string_literals(expression)
        for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_\\.]*", stripped):
            name = match.group(0)
            if name in reserved_literals or name in cel_builtins:
                continue
            # Single-letter identifiers are allowed (loop variables in
            # quantifier expressions like exists(x, items, x > 0))
            if len(name) == 1:
                continue
            # Namespace-prefixed references are usually valid — but
            # ``s.<name>`` references need an extra check: if <name>
            # is a known step input but NOT also a real workflow
            # signal, the author has chosen the wrong namespace
            # (s.* never holds step inputs at runtime).
            parts = name.split(".")
            root = parts[0]
            if root in namespace_roots:
                if (
                    root in {"s", "signal"}
                    and len(parts) >= 2  # noqa: PLR2004
                    and parts[1] in self.inputs_by_slug
                    and parts[1] not in self.workflow_signal_names
                ):
                    misnamespaced_inputs.add(parts[1])
                continue
            unknown.add(name)

        if misnamespaced_inputs:
            example = sorted(misnamespaced_inputs)[0]
            raise ValidationError(
                {
                    "cel_expression": _(
                        "Step inputs live in the i.* namespace, not s.* "
                        "— at runtime s.* contains only workflow signal "
                        "mappings and promoted upstream outputs, never "
                        "step inputs. Use i.%(first)s instead of "
                        "s.%(first)s (the assertion would silently read "
                        "null otherwise). Affected: %(names)s"
                    )
                    % {
                        "first": example,
                        "names": ", ".join(
                            f"s.{n}" for n in sorted(misnamespaced_inputs)
                        ),
                    },
                },
            )

        if unknown:
            raise ValidationError(
                {
                    "cel_expression": _(
                        "Bare identifiers are not allowed. Use p.%(first)s "
                        "for payload data or s.%(first)s for workflow signals. "
                        "Unknown: %(names)s"
                    )
                    % {
                        "first": sorted(unknown)[0],
                        "names": ", ".join(sorted(unknown)),
                    },
                }
            )

    def _resolve_target_data_path(self):
        catalog_choice = (self.cleaned_data.get("target_catalog_entry") or "").strip()
        value = (self.cleaned_data.get("target_data_path") or "").strip()

        # Hidden catalog dropdown selection (output signals only).
        if catalog_choice:
            sig = self.choice_map.get(catalog_choice)
            if not sig:
                raise ValidationError(
                    {
                        "target_data_path": _(
                            "Use s.<name> for workflow signals, p.<path> for "
                            "payload data, or o.<name> for step outputs."
                        ),
                    },
                )
            self.cleaned_data["resolved_signal"] = sig
            self.cleaned_data["target_catalog_entry"] = None
            self.cleaned_data["target_data_path_value"] = ""
            self.cleaned_data["resolved_stage"] = CatalogRunStage(sig.direction)
            return

        if not value:
            raise ValidationError(
                {
                    "target_data_path": _(
                        "Enter a target path. Use s.<name> for workflow signals, "
                        "p.<path> for payload data, or o.<name> for step outputs."
                    ),
                },
            )

        # ── Output prefix: o. / output. ──────────────────────────────
        if value.startswith(("o.", "output.")):
            name = value.split(".", 1)[1]
            if name in self.outputs_by_slug:
                self._set_resolved(
                    signal=self.outputs_by_slug[name],
                    stage=CatalogRunStage.OUTPUT,
                )
                return
            # Fall through to custom target check for unrecognised
            # output names (e.g. FMU outputs not in outputs_by_slug).
            if not self._validator_allows_custom_targets():
                raise ValidationError(
                    {
                        "target_data_path": _(
                            "Unknown output '%(name)s'. Check the "
                            "Step Outputs tab for available names."
                        )
                        % {"name": name},
                    },
                )
            self._set_resolved(
                path=name,
                stage=CatalogRunStage.OUTPUT,
            )
            return

        # ── Signal prefix: s. / signal. ─────────────────────────────
        # These reference either:
        #   1. Validator input signals (resolve to a StepIODefinition)
        #   2. Workflow-level signals — signal mappings or promoted
        #      outputs from upstream steps (stored as bare name)
        if value.startswith(("s.", "signal.")):
            name = value.split(".", 1)[1]
            if name in self.inputs_by_slug:
                self._set_resolved(
                    signal=self.inputs_by_slug[name],
                    stage=CatalogRunStage.INPUT,
                )
                return
            # Workflow-level signals (mappings + promoted outputs) are
            # always valid s.* targets regardless of whether the
            # validator allows custom assertion targets.
            if name in self.workflow_signal_names:
                self._set_resolved(
                    path=name,
                    stage=CatalogRunStage.INPUT,
                )
                return
            # Not a known validator input or workflow signal.
            if not self._validator_allows_custom_targets():
                raise ValidationError(
                    {
                        "target_data_path": _(
                            "Unknown signal '%(name)s'. Check the "
                            "Step Inputs tab or Signal Mappings "
                            "for available names."
                        )
                        % {"name": name},
                    },
                )
            self._set_resolved(
                path=name,
                stage=CatalogRunStage.INPUT,
            )
            return

        # ── Input prefix: i. / input. ───────────────────────────────
        # Per ADR-2026-05-22, step-local input values (parser-extracted
        # facts and resolved bindings) live in the i.* namespace. An
        # author who writes i.zone_count into the target field expects
        # it to resolve to the INPUT-direction StepIODefinition, not
        # error out as an unknown prefix. Without this branch, i.*
        # autocomplete entries from get_catalog_choices would fail
        # form validation — per the May 2026 review's P2 finding.
        if value.startswith(("i.", "input.")):
            name = value.split(".", 1)[1]
            if name in self.inputs_by_slug:
                # Known input definition — resolve to the SignalDefinition
                # row. The display layer renders this as i.<contract_key>
                # because target_signal_definition.direction is INPUT
                # (Assertion.target_display handles the prefix).
                self._set_resolved(
                    signal=self.inputs_by_slug[name],
                    stage=CatalogRunStage.INPUT,
                )
                return
            # Unknown input name — fall back to custom-path semantics
            # if the validator allows it; otherwise reject with a
            # clear error pointing to the Step Inputs panel.
            if not self._validator_allows_custom_targets():
                raise ValidationError(
                    {
                        "target_data_path": _(
                            "Unknown step input '%(name)s'. Check the "
                            "Step Inputs panel for available names."
                        )
                        % {"name": name},
                    },
                )
            self._set_resolved(
                path=value,  # preserve full i.<name> path for custom case
                stage=CatalogRunStage.INPUT,
            )
            return

        # ── Payload prefix: p. / payload. ────────────────────────────
        # References raw submission data.  Strip the prefix so the
        # evaluator resolves against the payload dict directly.
        if value.startswith(("p.", "payload.")):
            path_part = value.split(".", 1)[1]
            if not self._is_valid_target_path(path_part):
                raise ValidationError(
                    {
                        "target_data_path": _(
                            "Invalid path syntax after prefix. Use "
                            "dot notation (e.g. p.building.floor_area) "
                            "or bracket indexes (e.g. p.zones[0].temp)."
                        ),
                    },
                )
            self._set_resolved(
                path=path_part,
                stage=CatalogRunStage.INPUT,
            )
            return

        # ── Bare name (no recognised prefix) ─────────────────────────
        if not self._validator_allows_custom_targets():
            raise ValidationError(
                {
                    "target_data_path": _(
                        "Use s.<name> for workflow signals, p.<path> for "
                        "payload data, or o.<name> for step outputs."
                    ),
                },
            )
        if not self._is_valid_target_path(value):
            raise ValidationError(
                {
                    "target_data_path": _(
                        "Custom targets must use dot notation with optional "
                        "numeric indexes (e.g. `data.results[0].value`) or "
                        "JSONPath filter expressions "
                        "(e.g. `items[?@.name=='x'].value`).",
                    ),
                },
            )
        self._set_resolved(path=value, stage=CatalogRunStage.OUTPUT)

    def _set_resolved(
        self,
        *,
        signal=None,
        path: str = "",
        stage: CatalogRunStage = CatalogRunStage.OUTPUT,
    ) -> None:
        """Store the resolved target in cleaned_data."""
        self.cleaned_data["resolved_signal"] = signal
        self.cleaned_data["target_catalog_entry"] = None
        self.cleaned_data["target_data_path_value"] = path
        self.cleaned_data["resolved_stage"] = stage

    @staticmethod
    def _is_valid_target_path(value: str) -> bool:
        """Accept traditional dot/bracket paths or JSONPath filter expressions."""
        if "[?" not in value:
            return bool(CUSTOM_ASSERTION_TARGET_PATTERN.match(value))
        try:
            from validibot.validations.services._jsonpath_env import (
                validate_jsonpath_syntax,
            )

            validate_jsonpath_syntax(value)
        except ValueError:
            return False
        return True

    def _validator_allows_custom_targets(self) -> bool:
        return bool(getattr(self.validator, "allow_custom_assertion_targets", False))

    # NOTE: ``_reject_namespaced_basic_target`` was removed in Phase 5
    # of the May 2026 cleanup. The BASIC + i.*/s.* runtime trap it
    # guarded against is now fixed at the validator base layer via
    # ``BaseValidator._enrich_basic_payload`` — the validator merges
    # resolved bindings, workflow signals, and output signals into
    # the BASIC payload by their bare contract_key before evaluation.
    # The form-side rejection is no longer needed because the
    # runtime trap no longer exists.

    def _build_basic_payload(
        self,
        operator: AssertionOperator,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        options = self._collect_common_options()
        if operator in {
            AssertionOperator.EQ,
            AssertionOperator.NE,
            AssertionOperator.LT,
            AssertionOperator.LE,
            AssertionOperator.GT,
            AssertionOperator.GE,
            AssertionOperator.LEN_EQ,
            AssertionOperator.LEN_LE,
            AssertionOperator.LEN_GE,
            AssertionOperator.TYPE_IS,
        }:
            literal = self._require_literal("comparison_value")
            rhs = {"value": literal.value}
            if operator == AssertionOperator.TYPE_IS:
                rhs["value"] = str(literal.source)
            return rhs, options

        if operator in {
            AssertionOperator.BETWEEN,
            AssertionOperator.COUNT_BETWEEN,
        }:
            lower = self._require_literal("comparison_value")
            upper = self._require_literal("comparison_value_secondary")
            rhs = {"min": lower.value, "max": upper.value}
            options["include_min"] = bool(self.cleaned_data.get("include_min"))
            options["include_max"] = bool(self.cleaned_data.get("include_max"))
            return rhs, options

        if operator in {
            AssertionOperator.IN,
            AssertionOperator.NOT_IN,
            AssertionOperator.SUBSET,
            AssertionOperator.SUPERSET,
        }:
            rhs = {"values": self._extract_list_literals()}
            return rhs, options

        if operator == AssertionOperator.UNIQUE:
            return {}, options

        if operator in {
            AssertionOperator.CONTAINS,
            AssertionOperator.NOT_CONTAINS,
            AssertionOperator.STARTS_WITH,
            AssertionOperator.ENDS_WITH,
        }:
            literal = self._require_literal("comparison_value", prefer_number=False)
            return {"value": literal.value}, options

        if operator == AssertionOperator.MATCHES:
            pattern = (self.cleaned_data.get("regex_pattern") or "").strip()
            if not pattern:
                raise ValidationError(
                    {"regex_pattern": _("Provide a regular expression.")},
                )
            return {"pattern": pattern}, options

        if operator in {AssertionOperator.IS_NULL, AssertionOperator.NOT_NULL}:
            return {}, options

        if operator in {AssertionOperator.IS_EMPTY, AssertionOperator.NOT_EMPTY}:
            return {}, options

        if operator == AssertionOperator.APPROX_EQ:
            base = self._require_literal("comparison_value")
            tolerance = self.cleaned_data.get("tolerance_value")
            if tolerance is None:
                raise ValidationError(
                    {"tolerance_value": _("Provide a tolerance value.")},
                )
            mode = self.cleaned_data.get("tolerance_mode")
            if not mode:
                raise ValidationError(
                    {"tolerance_mode": _("Select a tolerance type.")},
                )
            rhs = {"value": base.value, "tolerance": float(tolerance)}
            options["tolerance_mode"] = mode
            return rhs, options

        if operator in {AssertionOperator.BEFORE, AssertionOperator.AFTER}:
            dt_value = self.cleaned_data.get("datetime_value")
            if not dt_value:
                raise ValidationError(
                    {"datetime_value": _("Provide a date/time value.")},
                )
            return {"value": dt_value.isoformat()}, options

        if operator == AssertionOperator.WITHIN:
            literal = self._require_literal("comparison_value", prefer_number=False)
            return {"value": literal.value}, options

        if operator in {
            AssertionOperator.ANY,
            AssertionOperator.ALL,
            AssertionOperator.NONE,
        }:
            nested_operator = self.cleaned_data.get("collection_operator")
            if not nested_operator:
                raise ValidationError(
                    {
                        "collection_operator": _(
                            "Select a condition for the collection members.",
                        ),
                    },
                )
            nested_literal = self._require_literal("collection_value")
            rhs = {
                "operator": nested_operator,
                "value": nested_literal.value,
            }
            return rhs, options

        raise ValidationError(
            {"operator": _("Operator %(op)s is not supported.") % {"op": operator}}
        )

    def _collect_common_options(self) -> dict[str, Any]:
        option_fields = (
            "case_insensitive",
            "unicode_fold",
            "coerce_types",
            "treat_missing_as_null",
        )
        options: dict[str, Any] = {}
        for field in option_fields:
            if self.cleaned_data.get(field):
                options[field] = True
        return options

    def _require_literal(
        self,
        field_name: str,
        *,
        prefer_number: bool = True,
    ) -> _LiteralValue:
        raw = self.cleaned_data.get(field_name)
        if raw is None:
            raw = ""
        raw_text = str(raw).strip()
        if not raw_text:
            raise ValidationError(
                {field_name: _("Provide a value.")},
            )
        if prefer_number:
            try:
                return _LiteralValue(float(Decimal(raw_text)), raw_text)
            except InvalidOperation:
                pass
        lowered = raw_text.lower()
        if lowered in {"true", "false"}:
            return _LiteralValue(lowered == "true", raw_text)
        if lowered == "null":
            return _LiteralValue(None, raw_text)
        return _LiteralValue(raw_text, raw_text)

    def _extract_list_literals(self) -> list[Any]:
        raw = self.cleaned_data.get("list_values") or ""
        values = [line.strip() for line in raw.splitlines() if line.strip()]
        if not values:
            raise ValidationError(
                {"list_values": _("Provide at least one value (one per line).")},
            )
        normalized = [self._require_literal_for_list(value).value for value in values]
        return normalized

    def _require_literal_for_list(self, value: str) -> _LiteralValue:
        try:
            return _LiteralValue(float(Decimal(value)), value)
        except InvalidOperation:
            lowered = value.lower()
            if lowered in {"true", "false"}:
                return _LiteralValue(lowered == "true", value)
            if lowered == "null":
                return _LiteralValue(None, value)
            return _LiteralValue(value, value)

    def _clean_cel_expression(self) -> str:
        expression = (self.cleaned_data.get("cel_expression") or "").strip()
        if not expression:
            raise ValidationError(
                {"cel_expression": _("Provide a CEL expression.")},
            )
        # Bound length before any regex work to make the polynomial
        # ReDoS surface in ``_find_unknown_cel_slugs`` a non-issue.
        # 4 KB is generous; legitimate CEL expressions are typically
        # well under 500 characters.
        if len(expression) > _MAX_CEL_EXPRESSION_LEN:
            raise ValidationError(
                {
                    "cel_expression": _(
                        "Expression is too long "
                        "(%(length)d characters; max %(limit)d).",
                    )
                    % {
                        "length": len(expression),
                        "limit": _MAX_CEL_EXPRESSION_LEN,
                    },
                },
            )
        if not self._delimiters_balanced(expression):
            raise ValidationError(
                {
                    "cel_expression": _(
                        "Parentheses and brackets must be balanced.",
                    ),
                },
            )
        # Always enforce namespace prefixes — bare identifiers fail at
        # runtime since payload keys are no longer promoted.
        unknown = self._find_unknown_cel_slugs(expression)
        if unknown:
            raise ValidationError(
                {
                    "cel_expression": _(
                        "Bare identifiers are not allowed. Use p.%(first)s "
                        "for payload data or s.%(first)s for workflow signals. "
                        "Unknown: %(names)s"
                    )
                    % {
                        "first": sorted(unknown)[0],
                        "names": ", ".join(sorted(unknown)),
                    },
                },
            )
        return expression

    def _delimiters_balanced(self, expression: str) -> bool:
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack: list[str] = []
        for char in expression:
            if char in pairs:
                stack.append(pairs[char])
            elif char in pairs.values():
                if not stack or stack.pop() != char:
                    return False
        return not stack

    def _find_unknown_cel_slugs(self, expression: str) -> set[str]:
        """Find identifiers not using the namespaced convention.

        Under the four-namespace CEL design, all data access must use
        a namespace prefix (``p.``, ``s.``, ``output.``, ``steps.``).
        This method returns bare identifiers that aren't CEL builtins,
        reserved literals, or single-letter loop variables.
        """
        reserved = {
            "true",
            "false",
            "null",
            "p",
            "payload",
            "s",
            "signal",
            "i",
            "input",
            "o",
            "output",
            "steps",
        }
        cel_builtins = {
            "has",
            "exists",
            "exists_one",
            "all",
            "map",
            "filter",
            "size",
            "contains",
            "startsWith",
            "endsWith",
            "type",
            "int",
            "double",
            "string",
            "bool",
            "abs",
            "ceil",
            "floor",
            "round",
            "timestamp",
            "duration",
            "matches",
            "in",
        }
        # Fold in the canonical custom-helper names (is_int, mean, …, plus
        # the V1 tabular helpers is_iso8601/parse_date/is_finite/now) from
        # the single source so this allowlist never drifts from the others.
        cel_builtins |= CUSTOM_HELPER_NAMES
        # Strip string literals (including escaped quotes) so identifiers
        # inside quotes are not treated as bare identifiers.
        stripped = _strip_cel_string_literals(expression)
        identifiers = {
            match.group(0)
            for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_\.]*", stripped)
        }
        namespace_roots = {
            "p",
            "payload",
            "s",
            "signal",
            "i",
            "input",
            "o",
            "output",
            "steps",
        }
        # The row.* namespace is only valid on a Tabular Validator step (its
        # row-stage CEL loop binds it). Scoped here so a stray ``row.x`` on a
        # JSON/XML step is still flagged. (col.* is deferred with V2 column
        # assertions, so it stays rejected for now.)
        if self._validator_is_tabular():
            namespace_roots.add("row")
        unknown = set()
        for ident in identifiers:
            if ident in reserved or ident in cel_builtins:
                continue
            if len(ident) == 1:
                continue
            root = ident.split(".")[0]
            if root in namespace_roots:
                continue
            unknown.add(ident)
        return unknown

    def _build_cel_preview(
        self,
        operator: str,
        rhs: dict[str, Any],
        options: dict[str, Any],
        when_expression: str,
    ) -> str:
        left = self._target_identifier()
        op = AssertionOperator(operator)
        expr = ""
        literal = rhs.get("value")
        formatter = self._format_literal
        if op == AssertionOperator.LE:
            expr = f"{left} <= {formatter(literal)}"
        elif op == AssertionOperator.LT:
            expr = f"{left} < {formatter(literal)}"
        elif op == AssertionOperator.GE:
            expr = f"{left} >= {formatter(literal)}"
        elif op == AssertionOperator.GT:
            expr = f"{left} > {formatter(literal)}"
        elif op == AssertionOperator.EQ:
            expr = f"{left} == {formatter(literal)}"
        elif op == AssertionOperator.NE:
            expr = f"{left} != {formatter(literal)}"
        elif op == AssertionOperator.BETWEEN:
            min_cmp = ">=" if options.get("include_min", True) else ">"
            max_cmp = "<=" if options.get("include_max", True) else "<"
            expr = (
                f"({left} {min_cmp} {formatter(rhs.get('min'))} && "
                f"{left} {max_cmp} {formatter(rhs.get('max'))})"
            )
        elif op == AssertionOperator.MATCHES:
            expr = f"re.matches({formatter(rhs.get('pattern'))}, {left})"
        elif op == AssertionOperator.IN:
            values = ", ".join(formatter(val) for val in rhs.get("values", []))
            expr = f"{left} in [{values}]"
        elif op == AssertionOperator.NOT_IN:
            values = ", ".join(formatter(val) for val in rhs.get("values", []))
            expr = f"!({left} in [{values}])"
        elif op == AssertionOperator.CEL_EXPR:
            expr = rhs.get("expr", "")
        else:
            expr = ""
        if when_expression:
            expr = f"({when_expression}) && ({expr})" if expr else when_expression
        return expr.strip()

    def _target_identifier(self) -> str:
        signal = self.cleaned_data.get("resolved_signal")
        if signal:
            # Per ADR-2026-05-22, INPUT-direction targets live in i.*,
            # OUTPUT in o.*. The previous "s." for INPUT was always
            # wrong — step inputs were never in s.* (which is reserved
            # for workflow vocabulary).
            if signal.direction == SignalDirection.OUTPUT:
                return f"o.{signal.contract_key}"
            return f"i.{signal.contract_key}"
        return self.cleaned_data.get("target_data_path_value") or ""

    def _format_literal(self, value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        escaped = str(value).replace('"', r"\"")
        return f'"{escaped}"'

    @classmethod
    def initial_from_instance(cls, assertion):
        initial = {
            "assertion_type": assertion.assertion_type,
            "severity": assertion.severity,
            "when_expression": assertion.when_expression,
            "message_template": assertion.message_template,
            "success_message": assertion.success_message,
        }
        if assertion.target_signal_definition_id:
            sig = assertion.target_signal_definition
            initial["target_catalog_entry"] = f"{sig.direction}:{sig.contract_key}"
            # Per ADR-2026-05-22: INPUT-direction targets render as
            # i.<slug>, OUTPUT as o.<slug>. The legacy "s." rendering
            # for INPUT was always wrong (step inputs are not signals).
            if sig.direction == SignalDirection.OUTPUT:
                initial["target_data_path"] = f"o.{sig.contract_key}"
            else:
                initial["target_data_path"] = f"i.{sig.contract_key}"
        else:
            initial["target_data_path"] = assertion.target_data_path
        if assertion.assertion_type == AssertionType.SHACL:
            rhs = assertion.rhs or {}
            initial["shacl_description"] = rhs.get("description", "")
            initial["shacl_target_graph"] = rhs.get("target_graph", "data")
            initial["shacl_query"] = rhs.get("query", "")
        elif assertion.assertion_type == AssertionType.BASIC:
            initial["operator"] = assertion.operator
            rhs = assertion.rhs or {}
            options = assertion.options or {}
            cls._apply_operator_initial(initial, assertion.operator, rhs, options)
        else:
            initial["cel_expression"] = (assertion.rhs or {}).get("expr", "")
        return initial

    @staticmethod
    def _apply_operator_initial(initial, operator, rhs, options):
        if operator in {
            AssertionOperator.EQ,
            AssertionOperator.NE,
            AssertionOperator.LT,
            AssertionOperator.LE,
            AssertionOperator.GT,
            AssertionOperator.GE,
            AssertionOperator.LEN_EQ,
            AssertionOperator.LEN_LE,
            AssertionOperator.LEN_GE,
            AssertionOperator.TYPE_IS,
        }:
            initial["comparison_value"] = rhs.get("value")
        elif operator in {
            AssertionOperator.BETWEEN,
            AssertionOperator.COUNT_BETWEEN,
        }:
            initial["comparison_value"] = rhs.get("min")
            initial["comparison_value_secondary"] = rhs.get("max")
            initial["include_min"] = options.get("include_min", True)
            initial["include_max"] = options.get("include_max", True)
        elif operator in {
            AssertionOperator.IN,
            AssertionOperator.NOT_IN,
            AssertionOperator.SUBSET,
            AssertionOperator.SUPERSET,
        }:
            values = rhs.get("values") or []
            initial["list_values"] = "\n".join(str(value) for value in values)
        elif operator == AssertionOperator.MATCHES:
            initial["regex_pattern"] = rhs.get("pattern")
        elif operator == AssertionOperator.APPROX_EQ:
            initial["comparison_value"] = rhs.get("value")
            initial["tolerance_value"] = rhs.get("tolerance")
            initial["tolerance_mode"] = options.get("tolerance_mode")
        elif operator in {AssertionOperator.BEFORE, AssertionOperator.AFTER}:
            initial["datetime_value"] = rhs.get("value")
        elif operator in {
            AssertionOperator.ANY,
            AssertionOperator.ALL,
            AssertionOperator.NONE,
        }:
            initial["collection_operator"] = options.get("collection_operator")
            initial["collection_value"] = rhs.get("value")
        elif operator == AssertionOperator.CEL_EXPR:
            initial["cel_expression"] = rhs.get("expr", "")
        return initial


class ValidatorRuleForm(CelHelpLabelMixin, forms.Form):
    """Form for creating/updating validator-level default assertions (CEL only)."""

    name = forms.CharField(
        label=_("Name"),
        max_length=200,
    )
    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )
    rule_type = forms.ChoiceField(
        label=_("Assertion type"),
        choices=ValidatorRuleType.choices,
    )
    cel_expression = forms.CharField(
        label=_("CEL expression"),
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=_(
            "Enter a CEL expression that references step inputs and step outputs."
        ),
    )
    order = forms.IntegerField(
        label=_("Order"),
        min_value=0,
        required=False,
        initial=0,
        help_text=_("Lower numbers run first."),
    )
    signals = forms.MultipleChoiceField(
        label=_("Inputs/outputs referenced"),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        signal_choices = kwargs.pop("signal_choices", [])
        super().__init__(*args, **kwargs)
        self.fields["signals"].choices = signal_choices
        # Inputs/outputs are auto-detected from the CEL expression; render as read-only.
        self.fields["signals"].disabled = True
        self.fields["signals"].help_text = _(
            "Detected from the CEL expression and shown for reference."
        )
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("name", css_class="col-12 col-xl-8"),
                Column("order", css_class="col-12 col-xl-4"),
            ),
            "description",
            "rule_type",
            "signals",
            "cel_expression",
        )
        self._append_cel_help_to_label("cel_expression")
        cel_label = self.fields["cel_expression"].label
        # Use format_html to safely escape the label
        self.fields["cel_expression"].label = format_html(
            '<span class="w-100 d-block">{}</span>',
            cel_label,
        )

    def clean_rule_type(self):
        value = self.cleaned_data.get("rule_type")
        if value != ValidatorRuleType.CEL_EXPRESSION:
            raise ValidationError(_("Unsupported assertion type."))
        return value

    def clean_cel_expression(self):
        expr = (self.cleaned_data.get("cel_expression") or "").strip()
        if not expr:
            raise ValidationError(_("CEL expression is required."))
        return expr


class StepIODefinitionForm(forms.ModelForm):
    """Form for creating/updating validator input and output definitions."""

    class Meta:
        model = StepIODefinition
        fields = [
            "direction",
            "contract_key",
            "native_name",
            "label",
            "data_type",
            "description",
            "source_kind",
            "is_path_editable",
            "order",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        self.validator = kwargs.pop("validator", None)
        super().__init__(*args, **kwargs)
        self.validator = self.validator or getattr(self.instance, "validator", None)
        self.fields["label"].required = False
        self.fields["label"].widget = forms.HiddenInput()
        self.fields["label"].initial = ""
        self.fields["contract_key"].label = _("Name")
        self.fields["contract_key"].help_text = _(
            "Short, slug-form name (lowercase letters, numbers, hyphens) used in "
            "assertions and CEL expressions."
        )
        self.fields["contract_key"].error_messages["required"] = _(
            "Name is required.",
        )
        self.fields["description"].help_text = _(
            "A short description to help you remember what data "
            "this input or output represents."
        )
        self.fields["source_kind"].help_text = _(
            "How the signal's value is obtained: from a payload path "
            "the author configures, or internally by the validator."
        )
        self.fields["is_path_editable"].help_text = _(
            "Whether workflow authors can edit the source data path "
            "for this signal when configuring a step."
        )
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("direction", css_class="col-12 col-md-6"),
            ),
            Row(
                Column("contract_key", css_class="col-12 col-md-6"),
                Column("native_name", css_class="col-12 col-md-6"),
            ),
            Row(
                Column("data_type", css_class="col-12 col-md-6"),
                Column("source_kind", css_class="col-12 col-md-6"),
            ),
            "description",
            Row(
                Column("is_path_editable", css_class="col-12 col-md-6"),
                Column("order", css_class="col-12 col-md-6"),
            ),
        )

    def clean_contract_key(self):
        value = (self.cleaned_data.get("contract_key") or "").strip()
        if not value:
            raise ValidationError(_("Name is required."))
        suggested = slugify(value)
        if suggested != value:
            raise ValidationError(
                _(
                    "Use slug format (lowercase letters, "
                    "numbers, hyphens). Try: %(suggested)s"
                )
                % {"suggested": suggested or _("a-slug-name")}
            )
        if not self.validator:
            return value
        existing = StepIODefinition.objects.filter(
            validator=self.validator,
            contract_key=value,
        ).exclude(pk=self.instance.pk)
        if existing.exists():
            raise ValidationError(
                _("Name must be unique across inputs and outputs for this validator.")
            )
        return value

    def clean(self):
        cleaned = super().clean()
        cleaned["label"] = ""
        self.cleaned_data["label"] = ""
        return cleaned


class ValidatorResourceFileForm(forms.ModelForm):
    """
    Form for creating and editing validator resource files.

    On create, validates the uploaded file against the ResourceTypeConfig
    for the selected resource type (extension, size, magic bytes, header).
    On edit, only metadata fields are shown (file is not replaceable).
    """

    class Meta:
        model = ValidatorResourceFile
        fields = ("name", "resource_type", "file", "description", "is_default")

    def __init__(self, *args, **kwargs):
        self.validator = kwargs.pop("validator", None)
        self.is_edit = kwargs.pop("is_edit", False)
        super().__init__(*args, **kwargs)

        if self.is_edit:
            # File is not replaceable -- upload new, delete old
            del self.fields["file"]
            del self.fields["resource_type"]

        self.helper = FormHelper()
        self.helper.form_tag = False

        if not self.is_edit:
            # Filter resource type choices to those supported by this validator
            if self.validator:
                allowed = get_resource_types_for_validator(
                    self.validator.validation_type,
                )
                self.fields["resource_type"].choices = [
                    (value, label)
                    for value, label in self.fields["resource_type"].choices
                    if value in allowed
                ]
                if len(allowed) == 1:
                    self.fields["resource_type"].initial = allowed[0]

    def clean_file(self):
        """
        Validate the uploaded file against the ResourceTypeConfig.

        Validation chain:
        1. Extension check against allowed_extensions
        2. Size check against max_size_bytes
        3. Suspicious magic byte detection (reuses core/filesafety.py)
        4. Header content validation
        """
        from validibot.core.filesafety import detect_suspicious_magic

        uploaded = self.cleaned_data.get("file")
        if not uploaded:
            return uploaded

        resource_type = self.cleaned_data.get("resource_type") or self.data.get(
            "resource_type",
        )
        config = get_resource_type_config(resource_type)
        if not config:
            return uploaded

        # 1. Extension check
        filename = uploaded.name
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in config.allowed_extensions:
            allowed = ", ".join(f".{e}" for e in sorted(config.allowed_extensions))
            raise ValidationError(
                _("File type '.%(ext)s' is not allowed. Accepted: %(allowed)s.")
                % {"ext": ext, "allowed": allowed},
            )

        # 2. Size check
        if uploaded.size > config.max_size_bytes:
            max_mb = config.max_size_bytes / (1024 * 1024)
            raise ValidationError(
                _("File is too large (max %(max)s MB).") % {"max": int(max_mb)},
            )

        # 3. Suspicious magic bytes
        uploaded.seek(0)
        head = uploaded.read(4096)
        uploaded.seek(0)
        if detect_suspicious_magic(head):
            raise ValidationError(
                _("This file appears to be a binary archive or executable."),
            )

        # 4. Header content validation
        if config.header_validator and not config.header_validator(head):
            raise ValidationError(
                _("File content does not match expected format for %(type)s.")
                % {"type": config.description},
            )

        return uploaded

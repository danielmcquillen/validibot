from __future__ import annotations

import contextlib
import re
import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.db.models import Value
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel
from slugify import slugify

from validibot.core.models import CallbackReceiptStatus
from validibot.core.textsafety import sanitize_plain_text
from validibot.projects.models import Project
from validibot.submissions.constants import OutputRetention
from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import data_format_allowed_file_types
from validibot.submissions.models import Submission
from validibot.users.models import Organization
from validibot.users.models import User
from validibot.validations.constants import RULESET_ASSERTION_NOTES_MAX_LENGTH
from validibot.validations.constants import VALIDATION_RUN_SHORT_DESCRIPTION_MAX_LENGTH
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ComputeTier
from validibot.validations.constants import CustomValidatorType
from validibot.validations.constants import FMUProbeStatus
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.constants import SignalSourceKind
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.constants import ValidatorTrustTier
from validibot.validations.constants import ValidatorWeight
from validibot.validations.constants import XMLSchemaType
from validibot.validations.constants import get_resource_types_for_validator
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep

_INPUT_NAMESPACE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:i|input)\.[A-Za-z_]",
)

# Same shape as the input pattern, for the output (o./output.) and submission
# namespaces. The negative-lookbehind keeps ``info.x`` / ``ratio.x`` /
# ``my_submission.x`` from false-matching — only a true namespace root counts.
_OUTPUT_NAMESPACE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:o|output)\.[A-Za-z_]",
)
_SUBMISSION_NAMESPACE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])submission\.[A-Za-z_]",
)
# Constants (c./const.) — design-time-known literals (ADR-2026-06-18). Same
# shape and negative-lookbehind as the patterns above, so ``arc.x`` / ``func.x``
# don't false-match — only the ``c`` / ``const`` namespace roots count.
_CONSTANTS_NAMESPACE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:c|const)\.[A-Za-z_]",
)


def _expr_references_input_namespace(expr: str) -> bool:
    """Heuristic: does this CEL expression reference i.* or input.?

    Used by ``RulesetAssertion.resolved_run_stage`` to classify CEL
    expressions stored in ``rhs["expr"]``. An expression that mentions
    ``i.foo`` or ``input.bar`` IS an input-stage assertion (per
    ADR-2026-05-22). That's an opt-in reclassification — we only mark
    a CEL assertion as INPUT-stage when it explicitly references the
    new ``i.*`` namespace.

    Why opt-in rather than opt-out: preserving the historic default
    (CEL expressions without a target path are output-stage) avoids
    silently reclassifying existing assertions. An assertion using
    only ``s.*`` was previously output-stage; it continues to be
    output-stage. The new behaviour only kicks in when the author
    explicitly adopts ``i.*``.

    The negative-lookbehind ``(?<![A-Za-z0-9_])`` prevents false
    matches on identifiers that happen to start with ``i`` (e.g.,
    ``my.input.x``) — only true namespace prefixes count.
    """
    return bool(_INPUT_NAMESPACE_PATTERN.search(expr or ""))


def _expr_references_output_namespace(expr: str) -> bool:
    """Heuristic: does this text reference o.* or output.*?

    Used to keep a ``submission.*`` assertion OUTPUT-stage when it also
    needs output values (which only exist after the validator runs). See
    ``RulesetAssertion.resolved_run_stage``.
    """
    return bool(_OUTPUT_NAMESPACE_PATTERN.search(expr or ""))


def _expr_references_submission_namespace(expr: str) -> bool:
    """Heuristic: does this text reference the submission.* namespace?

    The submission envelope (ADR-2026-06-03b) is fixed at submission time, so
    an assertion that reads it can be evaluated as an early INPUT-stage gate —
    unless it ALSO reads ``o.*``/``output.*``. See
    ``RulesetAssertion.resolved_run_stage``.
    """
    return bool(_SUBMISSION_NAMESPACE_PATTERN.search(expr or ""))


def _expr_references_constants_namespace(expr: str) -> bool:
    """Heuristic: does this text reference the c.* / const.* namespace?

    A Constant (ADR-2026-06-18) is workflow-definition-derived and known at
    authoring time, so it adds NO runtime dependency: an assertion that reads a
    constant can be an early INPUT-stage gate — unless it ALSO reads
    ``o.*``/``output.*``. This is what keeps a constants-only CEL check (e.g.
    ``payload.cost == payload.energy * c.energy_price``) from needlessly waiting
    for an advanced validator's container to run. See
    ``RulesetAssertion.resolved_run_stage``.
    """
    return bool(_CONSTANTS_NAMESPACE_PATTERN.search(expr or ""))


def get_allowed_extensions_for_workflow(workflow) -> set[str]:
    """Collect allowed file extensions for all validators in a workflow.

    Returns a set of lowercase extensions (without leading dots) that are
    allowed for file uploads to this workflow. Reads from the config
    registry, falling back to empty for unknown validator types.
    """
    from validibot.validations.validators.base.config import get_config
    from validibot.workflows.models import WorkflowStep

    extensions: set[str] = set()
    steps = WorkflowStep.objects.filter(workflow=workflow).select_related("validator")
    for step in steps:
        validator = step.validator
        if not validator:
            continue
        cfg = get_config(validator.validation_type)
        if cfg:
            extensions.update(ext.lower() for ext in cfg.allowed_extensions)
    return extensions


def default_supported_file_types_for_validation(
    validation_type: str,
) -> list[str]:
    """Return the default supported file types for a validation type.

    Reads from the config registry. Falls back to deriving from data
    formats, or ``[JSON]`` if no config is registered.
    """
    from validibot.validations.validators.base.config import get_config

    cfg = get_config(validation_type)
    if cfg and cfg.supported_file_types:
        return list(cfg.supported_file_types)
    # Fallback: derive from data formats
    derived_formats = default_supported_data_formats_for_validation(validation_type)
    derived = supported_file_types_for_data_formats(derived_formats)
    return derived or [SubmissionFileType.JSON]


def default_supported_data_formats_for_validation(validation_type: str) -> list[str]:
    """Return the default supported data formats for a validation type.

    Reads from the config registry. Falls back to ``[JSON]`` if no config
    is registered.
    """
    from validibot.validations.validators.base.config import get_config

    cfg = get_config(validation_type)
    if cfg and cfg.supported_data_formats:
        return list(cfg.supported_data_formats)
    return [SubmissionDataFormat.JSON]


def supported_file_types_for_data_formats(data_formats: list[str]) -> list[str]:
    """
    Expand data formats to the submission file types that can carry them.
    """
    collected: list[str] = []
    for fmt in data_formats or []:
        for ft in data_format_allowed_file_types(fmt):
            if ft not in collected:
                collected.append(ft)
    return collected


class Ruleset(TimeStampedModel):
    """
    Reusable rule bundle (JSON Schema, XML schema, custom logic, etc.).

    Rules can be stored inline via ``rules_text`` or uploaded as ``rules_file``.
    Only one storage mechanism should be used at a time; helper ``rules``
    returns the effective rule definition as text.
    Can be global (org=None) or org-private.
    """

    class Meta:
        indexes = [
            models.Index(
                fields=[
                    "org",
                    "ruleset_type",
                ],
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "org",
                    "ruleset_type",
                    "name",
                    "version",
                ],
                name="uq_ruleset_org_ruleset_type_name_version",
            ),
        ]

    org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="rulesets",
    )

    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rulesets",
        help_text=_("The user who created this ruleset."),
    )

    name = models.CharField(max_length=200)

    ruleset_type = models.CharField(
        max_length=40,
        choices=RulesetType.choices,
        help_text=_("Type of validation ruleset, e.g. 'json_schema', 'xml_schema'"),
    )

    version = models.CharField(
        max_length=40,
        blank=True,
        default="",
    )

    rules_file = models.FileField(
        upload_to="rulesets/",
        blank=True,
        help_text=_(
            "Optional uploaded file containing the ruleset definition. "
            "Leave empty when pasting rules directly.",
        ),
    )

    rules_text = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Inline ruleset definition (for example, JSON Schema or XML schema text). "
            "Use this when you prefer to paste or store the "
            "rules without uploading a file.",
        ),
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Additional metadata about the ruleset (non-rule data only)."),
    )

    def clean(self):
        super().clean()

        has_file = bool(self.rules_file and getattr(self.rules_file, "name", None))
        has_text = bool((self.rules_text or "").strip())
        if has_file and has_text:
            raise ValidationError(
                {
                    "rules_file": _("Provide rules either as text or file, not both."),
                    "rules_text": _("Provide rules either as text or file, not both."),
                },
            )

        # Validate XML schema_type when this ruleset is for XML
        if self.ruleset_type == RulesetType.XML_SCHEMA:
            meta = dict(self.metadata or {})
            schema_type_raw = str(meta.get("schema_type") or "").strip()
            if not schema_type_raw:
                raise ValidationError(
                    {
                        "metadata": _(
                            "XML schema rulesets must define metadata['schema_type'].",
                        ),
                    },
                )
            schema_type = schema_type_raw.upper()
            if schema_type not in set(XMLSchemaType.values):
                raise ValidationError(
                    {
                        "metadata": _("Schema type '%(st)s' is not valid for %(rt)s.")
                        % {"st": schema_type_raw, "rt": self.ruleset_type},
                    },
                )
            meta["schema_type"] = schema_type
            self.metadata = meta

        if self.ruleset_type == RulesetType.JSON_SCHEMA:
            meta = dict(self.metadata or {})
            schema_type_raw = str(meta.get("schema_type") or "").strip()
            if not schema_type_raw:
                raise ValidationError(
                    {
                        "metadata": _(
                            "JSON schema rulesets must define metadata['schema_type'].",
                        ),
                    },
                )
            schema_type_value = schema_type_raw
            if schema_type_value not in set(JSONSchemaVersion.values):
                candidate = schema_type_raw.upper()
                if candidate in JSONSchemaVersion.__members__:
                    schema_type_value = JSONSchemaVersion[candidate].value
                else:
                    raise ValidationError(
                        {
                            "metadata": _(
                                "Schema type '%(st)s' is not valid for %(rt)s.",
                            )
                            % {"st": schema_type_raw, "rt": self.ruleset_type},
                        },
                    )
            meta["schema_type"] = schema_type_value
            self.metadata = meta

        # SCHEMATRON rulesets carry the author's uploaded .sch source in
        # rules_text/rules_file, exactly like schema rulesets carry their
        # schema — so they share the content-required rule below. (The
        # step-config form additionally checks the source is well-formed
        # XML with the Schematron root; execution only ever happens in the
        # sandboxed container because compiled Schematron is XSLT, i.e.
        # code — ADR-2026-07-01 D4/D8.)
        if self.ruleset_type in {
            RulesetType.JSON_SCHEMA,
            RulesetType.XML_SCHEMA,
            RulesetType.SCHEMATRON,
        }:
            if not has_file and not has_text:
                raise ValidationError(
                    {
                        "rules_text": _(
                            "Schema rulesets must include either inline "
                            "rules text or an uploaded file.",
                        ),
                    },
                )

        # SCHEMATRON rules are executable code (compiled Schematron is XSLT) and
        # ship INLINE in the run envelope (``schematron_text``), so they must be
        # stored as inline ``rules_text`` — the step-config form always
        # normalizes an uploaded .sch into text and clears ``rules_file``
        # (workflows/views_helpers). A file-backed row can therefore only come
        # from a non-form path (import, admin, API), and it would BOTH break the
        # inline-rules contract and dodge the hardened-XML guard below — so we
        # forbid it here. The inline rules then get the same authoring guard the
        # form applies, so a ruleset created outside the form cannot persist a
        # rules document carrying a DTD/XXE or a non-Schematron root. The
        # container re-guards regardless (engine.guard_rules, D8b); failing at
        # authoring is just the earlier, clearer error.
        if self.ruleset_type == RulesetType.SCHEMATRON:
            if has_file:
                raise ValidationError(
                    {
                        "rules_file": _(
                            "Schematron rulesets must store their rules as "
                            "inline text, not an uploaded file.",
                        ),
                    },
                )
            if has_text:
                from validibot.validations.validators.schematron.security import (
                    SchematronSecurityError,
                )
                from validibot.validations.validators.schematron.security import (
                    validate_schematron_source,
                )

                try:
                    validate_schematron_source(self.rules_text)
                except SchematronSecurityError as exc:
                    raise ValidationError({"rules_text": str(exc)}) from exc

        # ── Phase 3 task 10: ruleset immutability ───────────────────
        # A ruleset that's referenced by any step on a locked or used
        # workflow is part of those workflows' launch contract. Editing
        # its rules in place would silently re-write what previously-
        # launched runs were checking against. The policy mirrors
        # ``Workflow.requires_new_version_for_contract_edits`` — once
        # the contract is "in use", you must clone-to-new-version
        # rather than mutate.
        #
        # This gate runs LAST so the existing canonicalization above
        # (e.g. metadata['schema_type'] -> uppercase) settles before
        # we compare against the DB row. A no-op canonicalization
        # produces the same bytes either way and won't trip the gate.
        if self.pk and self.is_used_by_locked_workflow():
            self._raise_if_immutable_fields_changed()

    @property
    def rules(self) -> str:
        """
        Return the stored ruleset definition as text.

        Prefers inline ``rules_text`` and falls back to reading the uploaded file.
        """
        text = (self.rules_text or "").strip()
        if text:
            return text
        if self.rules_file:
            try:
                self.rules_file.open("rb")
                raw = self.rules_file.read()
            finally:
                with contextlib.suppress(Exception):
                    self.rules_file.close()
            if isinstance(raw, bytes):
                return raw.decode("utf-8", errors="replace")
            return str(raw or "")
        return ""

    @property
    def validator(self):
        """
        Returns the validator linked via any workflow step that references this ruleset.
        Falls back to None when the ruleset has not been attached yet.
        """
        from validibot.workflows.models import WorkflowStep

        step = (
            WorkflowStep.objects.filter(ruleset=self)
            .select_related("validator")
            .first()
        )
        validator = getattr(step, "validator", None)
        return validator

    # ADR-2026-04-27 Phase 3 task 10: fields whose mutation changes
    # what the ruleset actually checks against. Editing any of these
    # on a ruleset used by a locked workflow is silently rewriting
    # the rules a previously-launched run was operating under.
    #
    # Excluded (deliberately mutable):
    # - ``name``, ``version`` — cosmetic / identity. Renaming doesn't
    #   change rule meaning.
    # - ``user``, ``org`` — ownership; shouldn't change by edit but
    #   not part of "what's checked".
    IMMUTABLE_RULESET_FIELDS = (
        "rules_text",
        "rules_file",
        "metadata",
        "ruleset_type",
    )

    def is_used_by_locked_workflow(self) -> bool:
        """Return True if a versioned locked/run-having workflow uses this ruleset.

        Only considers the *direct* ``WorkflowStep.ruleset`` linkage —
        system validators' ``default_ruleset`` rows are managed by
        ``sync_validators`` and are protected by the ``semantic_digest``
        drift gate (Phase 3 Session B), so they're out of scope for
        this check.
        """
        # Local import — workflows.models imports validations.models,
        # so importing the other way at module-load time would cycle.
        from validibot.workflows.constants import WorkflowHistoryPolicy
        from validibot.workflows.models import WorkflowStep

        return (
            WorkflowStep.objects.filter(
                ruleset=self,
                workflow__history_policy=WorkflowHistoryPolicy.VERSIONED,
            )
            .filter(
                Q(workflow__is_locked=True)
                | Q(workflow__validation_runs__isnull=False),
            )
            .exists()
        )

    def _raise_if_immutable_fields_changed(self) -> None:
        """Compare against the DB row; raise ``ValidationError`` on diff.

        Called from :meth:`clean` only when ``is_used_by_locked_workflow``
        is true. The fresh DB fetch is the single extra query per save
        attempt — fine vs the trust value of catching silent mutation.

        FieldFile equality compares by ``name`` (the storage path), so
        replacing the file at a new path triggers the gate. Replacing
        bytes at the same path silently does not — that's the
        ``content_hash`` story addressed by task 11 below.
        """
        try:
            original = type(self).objects.get(pk=self.pk)
        except type(self).DoesNotExist:
            # Race: the row was deleted between clean() and the fetch.
            # Treat as no-op; the save will fail in its own right.
            return

        changed: list[str] = []
        for field in self.IMMUTABLE_RULESET_FIELDS:
            new = getattr(self, field, None)
            old = getattr(original, field, None)
            # Special case: FieldFile compares by .name — direct ==
            # works because FieldFile.__eq__ defers to the path.
            if new != old:
                changed.append(field)

        if changed:
            errors = {
                field: _(
                    "This ruleset is referenced by a workflow that has runs "
                    "(or is locked); its rules cannot change in place. "
                    "Create a new ruleset (or a new workflow version) to "
                    "modify them."
                )
                for field in changed
            }
            raise ValidationError(errors)


class RulesetAssertion(TimeStampedModel):
    """
    A single rule that the assertion evaluator checks against validation data.

    Each assertion belongs to a ``Ruleset``, which in turn is attached either
    to a ``Validator`` (as its ``default_ruleset`` - always evaluated) or to
    an individual ``WorkflowStep`` (evaluated only when that step runs).
    At evaluation time the validator merges both sources, with default assertions
    ordered first, and evaluates them in a single pass via
    ``evaluate_assertions_for_stage()``.

    Assertion types:

    - **BASIC** - structured: an ``operator`` (e.g. le, between, matches)
      paired with ``rhs`` (operand payload) and ``options`` (tolerance,
      inclusive bounds, case folding, etc.).
    - **CEL_EXPRESSION** - free-form: the CEL source lives in ``rhs["expr"]``
      and is evaluated directly by the CEL evaluator.
    - **SHACL** - SHACL-validator-only SPARQL ASK assertion. The ASK query
      and target graph live in ``rhs`` and are evaluated by ``SHACLValidator``
      after pySHACL produces the SHACL results graph.

    Targeting:

    Every assertion targets either a ``StepIODefinition`` (a known,
    typed signal published by the validator) *or* a free-form
    ``target_data_path`` - never both, enforced by the
    ``ck_ruleset_assertion_target_oneof`` check constraint.  The target
    also determines the ``resolved_run_stage`` (input vs output) that
    controls when the assertion fires.

    Messaging:

    ``message_template`` is rendered when the assertion *fails*;
    ``success_message`` when it *passes*.  Both support template variables
    from the evaluation context.

    Documentation:

    ``notes`` is a free-form, author-facing record of the rationale behind
    the assertion (which standard it enforces, why a threshold was chosen).
    Unlike the message templates it is never shown to data submitters; it
    travels with the assertion through export/import, version cloning, and
    the read API so the reasoning is preserved alongside the rule.
    """

    ruleset = models.ForeignKey(
        Ruleset,
        on_delete=models.CASCADE,
        related_name="assertions",
    )

    order = models.PositiveIntegerField(default=0)

    assertion_type = models.CharField(
        max_length=32,
        choices=AssertionType.choices,
        default=AssertionType.BASIC,
    )

    operator = models.CharField(
        max_length=32,
        choices=AssertionOperator.choices,
        default=AssertionOperator.LE,
        help_text=_("Structured operator used for BASIC assertions."),
    )

    target_signal_definition = models.ForeignKey(
        "validations.StepIODefinition",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ruleset_assertions",
        help_text=_("Reference to a signal definition when targeting a known signal."),
    )

    # A TextField, not a bounded CharField, on purpose. Besides custom
    # JSON-style paths (which are short), this column is reused as the
    # display source for CEL assertions: ``target_display`` returns it for
    # AssertionType.CEL_EXPRESSION, and the form stores the whole expression
    # here. CEL expressions are validated up to ``_MAX_CEL_EXPRESSION_LEN``
    # (4096 chars) at the form layer, so a 255-char cap here would reject a
    # perfectly valid expression at ``full_clean`` time — and, because the
    # target field is hidden for CEL assertions in the UI, the error would
    # land on an invisible field. Matching the column to the form's contract
    # removes that whole failure mode. (Evaluation and the edit form read the
    # canonical expression from ``rhs["expr"]``, never from here.)
    target_data_path = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Custom JSON-style path for validators that allow free-form "
            "targets. Supports dot notation, [index], and filter "
            "expressions (e.g., items[?@.name=='x'].value).",
        ),
    )

    severity = models.CharField(
        max_length=16,
        choices=Severity.choices,
        default=Severity.ERROR,
    )

    when_expression = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional CEL expression gating when the assertion evaluates."),
    )

    rhs = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Operator payload (values, range bounds, regex pattern, etc.)."),
    )

    options = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Operator options (inclusive bounds, tolerance "
            "metadata, case folding, etc.).",
        ),
    )

    message_template = models.TextField(
        blank=True,
        default="",
        help_text=_("Message rendered when the assertion fails."),
    )

    success_message = models.TextField(
        blank=True,
        default="",
        help_text=_("Message rendered when the assertion passes."),
    )

    # Author-facing rationale, not an end-user message. ``message_template`` and
    # ``success_message`` are shown to people submitting data; ``notes`` records
    # *why* the author wrote the assertion (the standard it enforces, the edge
    # case it guards) for whoever maintains the workflow later. Like the message
    # templates it is non-semantic — editing it never changes what the assertion
    # checks — so it stays off ``IMMUTABLE_ASSERTION_FIELDS`` and remains editable
    # on a locked ruleset.
    notes = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Internal notes on the rationale behind this assertion. "
            "Not shown to data submitters.",
        ),
        # Bounds the storage/DoS surface of a free-text field. A ``TextField``
        # does NOT add a length validator from ``max_length`` (unlike
        # ``CharField``), so the explicit ``MaxLengthValidator`` is what actually
        # enforces the cap during ``full_clean()`` — covering every write path
        # (assertion form, mutation service, AND VAF import). ``max_length`` is
        # kept so the cap is declared on the field and inherited by form fields.
        max_length=RULESET_ASSERTION_NOTES_MAX_LENGTH,
        validators=[MaxLengthValidator(RULESET_ASSERTION_NOTES_MAX_LENGTH)],
    )

    cel_cache = models.TextField(
        blank=True,
        default="",
        help_text=_("Normalized CEL preview generated from the stored payload."),
    )

    spec_version = models.PositiveIntegerField(
        default=1,
        help_text=_("Schema version for `rhs` and `options` payloads."),
    )

    class Meta:
        ordering = ["order", "pk"]
        constraints = [
            models.CheckConstraint(
                name="ck_ruleset_assertion_target_oneof",
                condition=(
                    Q(
                        target_signal_definition__isnull=False,
                        target_data_path="",
                    )
                    | Q(
                        target_signal_definition__isnull=True,
                    )
                ),
            ),
        ]
        indexes = [
            models.Index(fields=["ruleset", "order"]),
            models.Index(fields=["operator"]),
        ]

    def __str__(self):
        if self.target_signal_definition_id and self.target_signal_definition:
            target = self.target_signal_definition.contract_key
        else:
            target = self.target_data_path
        return f"{self.ruleset_id}:{self.operator}:{target or '?'}"

    @property
    def resolved_run_stage(self) -> CatalogRunStage:
        """Classify this assertion as input-stage or output-stage.

        Per ADR-2026-05-22, an assertion is INPUT-stage if it can
        evaluate before the validator's main process runs. The
        classification rules:

        - **Signal-definition target** → direction (INPUT or OUTPUT)
        - **Path target starting with ``p.``, ``payload.``, ``s.``,
          ``signal.``, ``i.``, ``input.``** → INPUT
        - **``submission.*`` (basic target or CEL expression)** → INPUT,
          UNLESS it also references ``o.*``/``output.*`` — the envelope is
          knowable before the run, so a submission-only rule is an early
          gate, but one that also reads outputs genuinely needs results
          (ADR-2026-06-03b)
        - **``c.*`` / ``const.*`` (basic target or CEL expression)** → INPUT,
          UNLESS it also references ``o.*``/``output.*`` — a Constant is a
          workflow-defined literal known at authoring time, so it adds no
          runtime dependency and is stage-neutral (ADR-2026-06-18)
        - **CEL expression that explicitly references ``i.*`` or
          ``input.*``** → INPUT (opt-in reclassification — the only
          case where we override the legacy output-stage default for
          CEL expressions, since explicit ``i.*`` usage signals author
          intent to gate on parser facts)
        - **Anything else** → OUTPUT (legacy default; preserves
          backwards-compatible classification of CEL assertions that
          use only ``s.*`` or ``p.*`` references without target paths)
        """
        if self.target_signal_definition_id and self.target_signal_definition:
            return CatalogRunStage(self.target_signal_definition.direction)
        path = (self.target_data_path or "").strip()
        # Input-stage prefixes: p./payload. (raw payload),
        # s./signal. (workflow vocab), and i./input. (this step's
        # inputs, per ADR-2026-05-22).
        if path.startswith(
            ("s.", "signal.", "p.", "payload.", "i.", "input."),
        ):
            return CatalogRunStage.INPUT

        # Namespaces knowable BEFORE the run classify INPUT-stage — UNLESS the
        # assertion also needs output values (o.*/output.*), which exist only
        # after the validator runs. Two such namespaces:
        #   - submission.* (ADR-2026-06-03b): the envelope is fixed at
        #     submission time.
        #   - c.* / const.* (ADR-2026-06-18): Constants are workflow-defined
        #     literals, so a constants-only check (e.g.
        #     ``payload.cost == payload.energy * c.energy_price``) adds NO
        #     runtime dependency and must not be forced to wait for container
        #     dispatch. Constants are stage-neutral: INPUT unless o.*/output.*
        #     is also referenced.
        # The text to inspect is the CEL expression (rhs["expr"]) for a CEL
        # assertion, otherwise the basic target path. A basic ``c.<name>`` /
        # ``submission.<name>`` target is a clean path that never mentions o.*,
        # so it classifies INPUT; a CEL ``c.x == o.y`` correctly stays OUTPUT.
        # This block only fires when one of these namespaces is actually
        # referenced, so it has no effect on any pre-existing assertion.
        is_cel = self.assertion_type == AssertionType.CEL_EXPRESSION
        cel_expr = ((self.rhs or {}).get("expr") or "").strip() if is_cel else ""
        early_text = cel_expr or path
        references_early_namespace = (
            early_text.startswith(("submission.", "c.", "const."))
            or _expr_references_submission_namespace(early_text)
            or _expr_references_constants_namespace(early_text)
        )
        if references_early_namespace and not _expr_references_output_namespace(
            early_text,
        ):
            return CatalogRunStage.INPUT

        # CEL expressions store their expression in rhs["expr"]. Only
        # reclassify as INPUT when the expression explicitly references
        # i./input. — this is the opt-in path that makes
        # `i.zone_count >= 1` fire before container dispatch without
        # silently reclassifying any pre-existing CEL assertion that
        # happens to reference only s.* (which was always output-stage
        # by default).
        if cel_expr and _expr_references_input_namespace(cel_expr):
            return CatalogRunStage.INPUT
        return CatalogRunStage.OUTPUT

    def clean(self):
        super().clean()

        # Notes are author-entered free text. They are stored verbatim and made
        # safe at render time by output escaping (template autoescaping / JSON),
        # so we do NOT strip markup here — doing so would mangle the comparison
        # and generic syntax notes are full of (e.g. "value <max>", "List<int>").
        # We only strip control characters / NUL (which can fail a Postgres text
        # insert) and normalize whitespace. Done in clean() rather than the form
        # so it covers every write path that runs full_clean(): the assertion
        # form/mutation service AND VAF import.
        self.notes = sanitize_plain_text(self.notes)

        signal_set = bool(self.target_signal_definition_id)
        path_set = bool((self.target_data_path or "").strip())
        if signal_set and path_set:
            raise ValidationError(
                {
                    "target_data_path": _(
                        "Provide either a signal target or a custom path"
                        " (but not both)."
                    )
                },
            )

        # ── Phase 3 task 10: assertion immutability ─────────────────
        # Adding, removing, or editing an assertion all change what
        # the parent ruleset checks. If that ruleset is referenced by
        # any step on a locked or used workflow, every previously-
        # launched run was operating WITHOUT the new assertion (or
        # WITH the old one). Treat the assertion's semantic fields the
        # same way as the parent ruleset's: locked once the contract is
        # in use.
        if self.ruleset_id and self.ruleset.is_used_by_locked_workflow():
            if self.pk:
                self._raise_if_immutable_fields_changed()
            else:
                # Adding a brand-new assertion to a locked ruleset.
                raise ValidationError(
                    _(
                        "Cannot add a new assertion to this ruleset: it "
                        "is referenced by a workflow that has runs (or is "
                        "locked). Add the assertion to a new ruleset or a "
                        "new workflow version instead.",
                    ),
                )

    # ADR-2026-04-27 Phase 3 task 10: fields whose mutation changes
    # how the assertion fires or how its result classifies. Editing
    # any of these on a locked ruleset would silently re-write what
    # past runs were checking against, or how their results were
    # categorised.
    #
    # Excluded (deliberately mutable):
    # - ``order`` — display position in the UI; doesn't change logic.
    # - ``message_template`` / ``success_message`` — operator-facing
    #   text. Improving wording shouldn't be blocked.
    # - ``cel_cache`` — compiled-CEL cache, recomputed lazily.
    IMMUTABLE_ASSERTION_FIELDS = (
        "assertion_type",
        "operator",
        "target_signal_definition_id",
        "target_data_path",
        "rhs",
        "options",
        "when_expression",
        "severity",
        "spec_version",
    )

    def _raise_if_immutable_fields_changed(self) -> None:
        """Compare against the DB row; raise on diff. See ``Ruleset`` twin."""
        try:
            original = type(self).objects.get(pk=self.pk)
        except type(self).DoesNotExist:
            return

        changed: list[str] = []
        for field in self.IMMUTABLE_ASSERTION_FIELDS:
            new = getattr(self, field, None)
            old = getattr(original, field, None)
            if new != old:
                changed.append(field)

        if changed:
            errors = {
                field: _(
                    "This assertion belongs to a ruleset used by a workflow "
                    "that has runs (or is locked); its rule cannot change "
                    "in place. Edit a copy on a new ruleset or workflow "
                    "version."
                )
                for field in changed
            }
            raise ValidationError(errors)

    @property
    def short_type_label(self) -> str:
        """Short, mechanism-oriented pill label for the assertion card.

        Replaces ``get_assertion_type_display()`` on the card view because
        the underlying enum label is the *validator family* ("SHACL",
        "Basic Assertion", "CEL expression") whereas what an author
        actually wants to see at a glance is the assertion *mechanism* —
        is this a SPARQL query, a CEL expression, or a plain comparison?

        - ``SHACL`` family → "SPARQL" (the assertion is always a SPARQL
          ASK; "SHACL" on a SHACL step is redundant)
        - ``CEL_EXPRESSION`` family → "CEL"
        - ``BASIC`` family → "Basic"

        Kept as a property so the template stays free of magic strings
        and the mapping has a single source of truth.
        """
        if self.assertion_type == AssertionType.SHACL:
            return _("SPARQL")
        if self.assertion_type == AssertionType.CEL_EXPRESSION:
            return _("CEL")
        return _("Basic")

    @property
    def target_display(self) -> str:
        if self.assertion_type == AssertionType.SHACL:
            description = (self.rhs or {}).get("description") or ""
            return description or _("SPARQL ASK")
        if self.assertion_type == AssertionType.CEL_EXPRESSION:
            # When the author gave the expression a description, show that on
            # the assertion card instead of the raw CEL. ``rhs["expr"]`` is
            # authoritative; ``target_data_path`` remains a compatibility
            # fallback for older rows created before the expression payload
            # was normalized.
            rhs = self.rhs or {}
            description = (rhs.get("description") or "").strip()
            expression = (rhs.get("expr") or "").strip()
            return description or expression or self.target_data_path
        if self.target_signal_definition_id and self.target_signal_definition:
            sig = self.target_signal_definition
            # Per ADR-2026-05-22: INPUT-direction targets live in i.*,
            # OUTPUT-direction in o.*. The previous "s." for INPUT was
            # always wrong — step inputs were never in the s.* namespace
            # (which is reserved for workflow vocabulary). See ADR-2026-05-22b
            # for the full terminology decision.
            prefix = "o." if sig.direction == SignalDirection.OUTPUT else "i."
            return f"{sig.label or sig.contract_key} ({prefix}{sig.contract_key})"
        return self.target_data_path

    @property
    def condition_display(self) -> str:
        if self.assertion_type == AssertionType.SHACL:
            target_graph = (self.rhs or {}).get("target_graph", "data")
            target_label = {
                "data": _("submitted RDF data graph"),
                "results": _("SHACL results graph"),
                "union": _("data + results graph"),
            }.get(target_graph, _("%(target)s graph") % {"target": target_graph})
            description = (self.rhs or {}).get("description") or ""
            if description:
                return _("SPARQL ASK against %(target)s") % {
                    "target": target_label,
                }
            return _("Against %(target)s") % {
                "target": target_label,
            }
        if self.assertion_type == AssertionType.CEL_EXPRESSION:
            # CEL expression is already shown via target_display (stored in
            # target_data_path). Returning it here would duplicate it on the card.
            return ""
        formatter = self._format_literal
        rhs = self.rhs or {}
        options = self.options or {}
        op = AssertionOperator(self.operator)
        if op == AssertionOperator.LE:
            return _("≤ %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.LT:
            return _("< %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.GE:
            return _("≥ %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.GT:
            return _("> %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.EQ:
            return _("Equals %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.NE:
            return _("Not equals %(value)s") % {"value": formatter(rhs.get("value"))}
        if op == AssertionOperator.BETWEEN:
            bounds = _(
                "%(min)s %(min_cmp)s target %(max_cmp)s %(max)s",
            ) % {
                "min": formatter(rhs.get("min")),
                "max": formatter(rhs.get("max")),
                "min_cmp": ">=" if options.get("include_min", True) else ">",
                "max_cmp": "<=" if options.get("include_max", True) else "<",
            }
            return bounds
        if op in {AssertionOperator.IN, AssertionOperator.NOT_IN}:
            values = ", ".join(formatter(v) for v in rhs.get("values", []))
            return (
                _("One of %(values)s")
                if op == AssertionOperator.IN
                else _("Not in %(values)s")
            ) % {"values": values}
        if op == AssertionOperator.MATCHES:
            return _("Matches %(pattern)s") % {"pattern": formatter(rhs.get("pattern"))}
        if op in {
            AssertionOperator.CONTAINS,
            AssertionOperator.NOT_CONTAINS,
            AssertionOperator.STARTS_WITH,
            AssertionOperator.ENDS_WITH,
        }:
            verb = self.get_operator_display()
            return _("%(verb)s %(value)s") % {
                "verb": verb,
                "value": formatter(rhs.get("value")),
            }
        if op in {AssertionOperator.IS_NULL, AssertionOperator.NOT_NULL}:
            return self.get_operator_display()
        if op == AssertionOperator.APPROX_EQ:
            tol = rhs.get("tolerance")
            return _("≈ %(value)s ± %(tol)s") % {
                "value": formatter(rhs.get("value")),
                "tol": formatter(tol),
            }
        return self.get_operator_display()

    def _format_literal(self, value) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return _("true") if value else _("false")
        return str(value)


def _default_validator_file_types() -> list[str]:
    return [SubmissionFileType.JSON]


def _default_validator_data_formats() -> list[str]:
    return [SubmissionDataFormat.JSON]


def _fmu_upload_path(instance: FMUModel, filename: str) -> str:
    org_segment = instance.org_id or "system"
    return f"fmu/{org_segment}/{uuid.uuid4()}-{filename}"


class FMUModel(TimeStampedModel):
    """
    Stored FMU artifact plus parsed metadata used by FMU validators.

    The FMU never executes inside Django; we store it for Modal runners to
    download and for offline inspection/probe runs. Each FMU also records a
    checksum and Modal Volume path so the Modal runtime can reuse a cached
    copy keyed by checksum.
    """

    class FMUKind(models.TextChoices):
        MODEL_EXCHANGE = "ModelExchange", _("Model Exchange")
        CO_SIMULATION = "CoSimulation", _("Co-Simulation")

    class FMUVersion(models.TextChoices):
        V2_0 = "2.0", _("FMI 2.0")
        V3_0 = "3.0", _("FMI 3.0")

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="fmu_models",
        null=True,
        blank=True,
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        related_name="fmu_models",
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    file = models.FileField(upload_to=_fmu_upload_path)
    checksum = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=_("SHA256 checksum used to reference cached FMUs in storage."),
    )
    gcs_uri = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text=_(
            "Cloud storage URI to the canonical FMU object "
            "(e.g., gs://bucket/fmus/<checksum>.fmu for GCS)."
        ),
    )
    fmu_version = models.CharField(
        max_length=8,
        choices=FMUVersion.choices,
        default=FMUVersion.V2_0,
    )
    kind = models.CharField(
        max_length=32,
        choices=FMUKind.choices,
        default=FMUKind.CO_SIMULATION,
    )
    is_approved = models.BooleanField(default=False)
    size_bytes = models.BigIntegerField(default=0)
    introspection_metadata = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.name} ({self.fmu_version}, {self.kind})"


class FMUVariable(TimeStampedModel):
    """
    Parsed variable metadata from modelDescription.xml attached to an FMUModel.

    These rows mirror ScalarVariable definitions from modelDescription.xml.
    """

    fmu_model = models.ForeignKey(
        FMUModel,
        on_delete=models.CASCADE,
        related_name="variables",
    )
    name = models.CharField(max_length=255)
    causality = models.CharField(max_length=64)
    variability = models.CharField(max_length=64, blank=True, default="")
    value_reference = models.BigIntegerField(default=0)
    value_type = models.CharField(max_length=64)
    unit = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(
                fields=[
                    "fmu_model",
                    "name",
                ],
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.name} ({self.causality})"


class FMUProbeResult(TimeStampedModel):
    """
    Tracks the latest probe state for an FMU prior to approval.

    Probe runs validate that an FMU can be opened safely and collect a clean
    variable snapshot for catalog seeding.
    """

    fmu_model = models.OneToOneField(
        FMUModel,
        on_delete=models.CASCADE,
        related_name="probe_result",
    )
    status = models.CharField(
        max_length=16,
        choices=FMUProbeStatus.choices,
        default=FMUProbeStatus.PENDING,
    )
    last_error = models.TextField(blank=True, default="")
    details = models.JSONField(default=dict, blank=True)

    def mark_failed(self, message: str, details: dict | None = None) -> None:
        self.status = FMUProbeStatus.FAILED
        self.last_error = message
        if details:
            self.details = details
        self.save(
            update_fields=[
                "status",
                "last_error",
                "details",
                "modified",
            ],
        )


class Validator(TimeStampedModel):
    """
    A pluggable validator family and integer revision.

    ``version`` is the Validibot contract revision for this validator row, not
    a domain-standard label such as an EnergyPlus, FMI, or JSON Schema version.
    Domain semantics belong in tags/metadata/capabilities; the integer keeps
    URL routing and "latest version" resolution deterministic.
    """

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["slug", "version"],
                name="uq_validator_slug_version",
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    "validation_type",
                    "slug",
                ],
            ),
            models.Index(
                fields=["availability_state", "validation_type"],
                name="val_validator_avail_type_idx",
            ),
        ]
        ordering = ["order", "name"]

    slug = models.SlugField(
        null=False,
        blank=True,
        help_text=_(
            "A unique identifier for the validator, used in URLs.",
        ),  # e.g. "json-2020-12", "eplus-23-1"
    )

    short_description = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "A brief summary of the validator's purpose. This description "
            "appears in lists and cards."
        ),
    )

    description = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional longer description of the validator."),
    )

    name = models.CharField(
        max_length=120,
        null=False,
        blank=False,
    )  # display label

    org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="validators",
        help_text=_(
            "Owning organization for custom validators (null for system validators).",
        ),
    )

    validation_type = models.CharField(
        max_length=80,
        null=False,
        blank=False,
        help_text=_(
            "Runtime validator type string. Built-in validators use "
            "ValidationType constants, but plugin validators may register "
            "additional strings at startup."
        ),
    )

    version = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        help_text=_(
            "Positive integer revision for this validator contract. Domain "
            "versions such as EnergyPlus/FMI/JSON Schema versions belong in "
            "tags or metadata, not this field."
        ),
    )

    default_ruleset = models.ForeignKey(
        Ruleset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    processor_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text=_(
            "The name of the process that generates output signals from input signals.",
        ),
    )

    has_processor = models.BooleanField(
        default=False,
        help_text=_(
            "True when the validator includes an intermediate "
            "processor that produces output signals."
        ),
    )
    supports_assertions = models.BooleanField(
        default=False,
        help_text=_(
            "True when this validator supports step-level assertions "
            "(Basic, CEL, or validator-specific assertion types)."
        ),
    )
    fmu_model = models.ForeignKey(
        "validations.FMUModel",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="validators",
        help_text=_(
            "FMU artifact backing this validator (only used for FMU validators).",
        ),
    )

    order = models.PositiveIntegerField(
        default=0,
        help_text=_("Relative ordering for display purposes."),
    )

    is_system = models.BooleanField(
        default=True,
        help_text=_("True when the validator ships with the platform."),
    )

    is_enabled = models.BooleanField(
        default=True,
        help_text=_(
            "Disabled validators are hidden from users and cannot be "
            "added to workflows. Toggle via the admin panel."
        ),
    )

    release_state = models.CharField(
        max_length=16,
        choices=ValidatorReleaseState.choices,
        default=ValidatorReleaseState.PUBLISHED,
        help_text=_(
            "Release state for system validators. DRAFT hides the validator, "
            "COMING_SOON shows it disabled, PUBLISHED makes it fully available."
        ),
    )

    availability_state = models.CharField(
        max_length=24,
        choices=ValidatorAvailabilityState.choices,
        default=ValidatorAvailabilityState.AVAILABLE,
        help_text=_(
            "Whether this validator's runtime config/class is available in "
            "the current deployment. Missing validators remain in the DB for "
            "history but cannot launch."
        ),
    )
    availability_message = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Operator-facing reason this validator is unavailable, if any.",
        ),
    )
    config_provider = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "Python provider module that last synced this validator from a "
            "ValidatorConfig. Empty means the row is not managed by plugin "
            "config reconciliation."
        ),
    )

    allow_custom_assertion_targets = models.BooleanField(
        default=False,
        help_text=_(
            "Allow assertions against data paths not declared as signals.",
        ),
    )

    # Custom validators should only select a single data format from the allowed set
    CUSTOM_VALIDATOR_ALLOWED_DATA_FORMATS = {
        SubmissionDataFormat.JSON,
        SubmissionDataFormat.YAML,
    }

    supported_data_formats = ArrayField(
        base_field=models.CharField(
            max_length=32,
            choices=SubmissionDataFormat.choices,
        ),
        default=_default_validator_data_formats,
        help_text=_(
            "Data formats this validator can parse (e.g., JSON, EnergyPlus IDF).",
        ),
    )

    supported_file_types = ArrayField(
        base_field=models.CharField(
            max_length=32,
            choices=SubmissionFileType.choices,
        ),
        default=_default_validator_file_types,
        help_text=_(
            "Logical file types this validator can process (JSON, XML, text, etc.).",
        ),
    )

    # Compute metering fields — used by the cloud billing system to classify
    # validators and calculate credit consumption. In the community edition
    # these are informational only.
    compute_tier = models.CharField(
        max_length=10,
        choices=ComputeTier.choices,
        default=ComputeTier.LOW,
        help_text=_(
            "Compute intensity classification. LOW = metered by launch count. "
            "HIGH = metered by credit consumption."
        ),
    )
    compute_weight = models.PositiveSmallIntegerField(
        default=ValidatorWeight.NORMAL,
        choices=ValidatorWeight.choices,
        help_text=_(
            "Credit multiplier for HIGH-compute validators. "
            "Higher weight = more credits consumed per minute of runtime."
        ),
    )

    # ADR-2026-04-27 Phase 3, tasks 8–9: a stable hash of this
    # validator's *semantic* fields (the things that change what it
    # does, excluding cosmetic strings + identity). Populated by
    # ``sync_validators`` from the ValidatorConfig at deploy time.
    # Mismatch under the same (slug, version) -> drift; sync fails
    # unless ``--allow-drift`` is set.
    # Kept blank=True for: (a) custom org validators that aren't
    # synced from a config, and (b) the migration backfill window
    # before the first sync runs.
    semantic_digest = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "SHA-256 of the semantic config at sync time. Used to "
            "detect drift when a validator's behavior changes under "
            "the same (slug, version) without an explicit version bump."
        ),
    )

    # ADR-2026-04-27 Phase 5 Session C: trust tier of the validator
    # backend container this validator dispatches to. TIER_1 (default)
    # is the first-party hardening profile we ship today. TIER_2 is
    # the stricter sandbox the runner applies for user-added or
    # partner-authored backends — egress allowlist or no-network,
    # tighter caps, gVisor/Kata runtime when available, cosign-signed
    # image required.
    #
    # The field lives on Validator (not on a separate ValidatorBackend
    # row) because the workflow-step → validator FK already addresses
    # what we need, and there's no separate backend table today. The
    # *meaning* is "the backend this validator points at" — simple
    # validators (no backend) keep TIER_1 by construction.
    trust_tier = models.CharField(
        max_length=16,
        choices=ValidatorTrustTier.choices,
        default=ValidatorTrustTier.TIER_1,
        help_text=_(
            "Trust tier of the validator backend container this validator "
            "dispatches to. TIER_1 is the first-party hardening profile "
            "(default); TIER_2 applies a stricter sandbox for user-added "
            "or partner-authored backends. Simple validators (no backend) "
            "stay at TIER_1 by construction."
        ),
    )

    @property
    def card_image_name(self) -> str:
        """Return the card image filename for the validator library UI.

        Reads from the config registry. Falls back to the default card
        image for validators without a registered config.
        """
        from validibot.validations.validators.base.config import get_config

        cfg = get_config(self.validation_type)
        if cfg:
            return cfg.card_image
        return "default_card_img_small.png"

    @property
    def display_icon(self) -> str:
        """Return the Bootstrap Icons CSS class for this validator type.

        Reads from the config registry. Falls back to a generic icon
        for validators without a registered config.
        """
        from validibot.validations.validators.base.config import get_config

        cfg = get_config(self.validation_type)
        if cfg:
            return cfg.icon
        return "bi-journal-bookmark"

    @property
    def is_published(self) -> bool:
        """Return True if validator is published and fully available."""
        return self.release_state == ValidatorReleaseState.PUBLISHED

    @property
    def is_coming_soon(self) -> bool:
        """Return True if validator is marked as coming soon."""
        return self.release_state == ValidatorReleaseState.COMING_SOON

    @property
    def is_draft(self) -> bool:
        """Return True if validator is in draft state (hidden)."""
        return self.release_state == ValidatorReleaseState.DRAFT

    @property
    def is_runtime_available(self) -> bool:
        """Return True when this validator can be executed in this process."""
        return self.availability_state == ValidatorAvailabilityState.AVAILABLE

    def runtime_unavailable_reason(self) -> str:
        """Return an operator-readable reason this validator cannot run."""
        if self.is_runtime_available:
            return ""
        if self.availability_message:
            return self.availability_message
        if self.availability_state == ValidatorAvailabilityState.MISSING_CONFIG:
            return _(
                "The validator plugin that registered this validator is not "
                "available in the current deployment."
            )
        if self.availability_state == ValidatorAvailabilityState.RETIRED:
            return _("This validator has been retired.")
        return _("This validator is not available in the current deployment.")

    def get_validation_type_display(self) -> str:
        """Return a label for dynamic validation type strings.

        Django only generates ``get_FOO_display()`` automatically when a field
        has static ``choices``. ``validation_type`` is intentionally dynamic, so
        keep the display API templates already use and source the label from the
        registered config when possible.
        """
        from validibot.validations.validators.base.config import get_config

        cfg = get_config(self.validation_type)
        if cfg:
            return cfg.name
        return self.name or self.validation_type

    @property
    def supports_resource_files(self) -> bool:
        """Return True if this validator type accepts resource files."""
        return bool(get_resource_types_for_validator(self.validation_type))

    def __str__(self):
        prefix = f"{self.validation_type}"
        if self.org_id:
            prefix = f"{self.org.name} · {self.validation_type}"
        return f"{prefix} {self.slug} v{self.version}".strip()

    def clean(self):
        super().clean()
        data_formats = [value for value in (self.supported_data_formats or []) if value]
        file_types_hint = [
            value for value in (self.supported_file_types or []) if value
        ]
        placeholder_formats = _default_validator_data_formats()
        placeholder_file_types = _default_validator_file_types()
        expected_formats = default_supported_data_formats_for_validation(
            self.validation_type,
        )
        # If formats/file types are still on the placeholder defaults, apply the
        # validation-type defaults so compatibility checks stay accurate.
        if not data_formats or (
            data_formats == placeholder_formats
            and expected_formats != placeholder_formats
            and (not file_types_hint or file_types_hint == placeholder_file_types)
        ):
            data_formats = expected_formats
        normalized_formats: list[str] = []
        for value in data_formats:
            if value not in SubmissionDataFormat.values:
                raise ValidationError(
                    {
                        "supported_data_formats": _(
                            "'%(value)s' is not a supported submission data format.",
                        )
                        % {"value": value},
                    },
                )
            if value not in normalized_formats:
                normalized_formats.append(value)
        if self.validation_type == ValidationType.CUSTOM_VALIDATOR:
            invalid = [
                value
                for value in normalized_formats
                if value not in self.CUSTOM_VALIDATOR_ALLOWED_DATA_FORMATS
            ]
            if invalid:
                raise ValidationError(
                    {
                        "supported_data_formats": _(
                            "Custom validators support only JSON or YAML."
                        ),
                    },
                )
            if len(normalized_formats) != 1:
                raise ValidationError(
                    {
                        "supported_data_formats": _(
                            "Select exactly one data format for a custom validator."
                        ),
                    },
                )
        self.supported_data_formats = normalized_formats

        derived_file_types = supported_file_types_for_data_formats(
            self.supported_data_formats,
        )
        file_types = [value for value in (self.supported_file_types or []) if value]
        if not file_types or file_types == _default_validator_file_types():
            file_types = default_supported_file_types_for_validation(
                self.validation_type,
            )
        normalized_files: list[str] = []
        for value in file_types:
            if value not in SubmissionFileType.values:
                raise ValidationError(
                    {
                        "supported_file_types": _(
                            "'%(value)s' is not a supported submission file type.",
                        )
                        % {"value": value},
                    },
                )
            if value not in normalized_files:
                normalized_files.append(value)
        for derived in derived_file_types:
            if derived not in normalized_files:
                normalized_files.append(derived)
        self.supported_file_types = normalized_files

        if self.validation_type == ValidationType.FMU:
            if not self.fmu_model_id:
                if not self.is_system:
                    raise ValidationError(
                        {
                            "fmu_model": _(
                                "Assign an FMU asset before saving an FMU validator.",
                            ),
                        },
                    )
        elif self.fmu_model_id:
            raise ValidationError(
                {
                    "fmu_model": _(
                        "FMU assets can only be attached to FMU validators.",
                    ),
                },
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        if not self.slug:
            base_slug = slugify(f"{self.name}")
            if self.org_id:
                base_slug = slugify(f"{self.org_id}-{self.name}")
            self.slug = base_slug
        if self.org_id:
            self.is_system = False
        super().save(*args, **kwargs)
        self.ensure_default_ruleset()

    def ensure_default_ruleset(self) -> Ruleset:
        """Ensure this validator has a default_ruleset, creating one if needed.

        The default ruleset holds validator-level assertions that run on every
        workflow step using this validator. It's auto-created on first save.

        Returns:
            The existing or newly created default Ruleset.
        """
        if self.default_ruleset_id:
            return self.default_ruleset

        # Map validation_type to RulesetType. They share the same values
        # except AI_ASSIST which falls back to BASIC.
        ruleset_type = self.validation_type
        if ruleset_type not in RulesetType.values:
            ruleset_type = RulesetType.BASIC

        ruleset = Ruleset.objects.create(
            name=f"{self.name} - Default Assertions",
            ruleset_type=ruleset_type,
            org=self.org,
        )
        # Use update() to avoid re-triggering save/full_clean
        Validator.objects.filter(pk=self.pk).update(default_ruleset=ruleset)
        self.default_ruleset = ruleset
        self.default_ruleset_id = ruleset.pk
        return ruleset

    @property
    def is_custom(self) -> bool:
        return bool(self.org_id and not self.is_system)

    def supports_file_type(self, file_type: str) -> bool:
        normalized = (file_type or "").lower()
        allowed = {value.lower() for value in (self.supported_file_types or [])}
        return normalized in allowed

    def supports_data_format(self, data_format: str) -> bool:
        normalized = (data_format or "").lower()
        allowed = {value.lower() for value in (self.supported_data_formats or [])}
        return normalized in allowed

    def supports_any_file_type(self, file_types: list[str]) -> bool:
        allowed = {value.lower() for value in (self.supported_file_types or [])}
        incoming = {value.lower() for value in file_types}
        return bool(allowed & incoming)

    def supported_file_type_labels(self) -> list[str]:
        labels: list[str] = []
        for value in self.supported_file_types or []:
            try:
                labels.append(str(SubmissionFileType(value).label))
            except Exception:
                labels.append(str(value))
        return labels

    def supported_data_format_labels(self) -> list[str]:
        labels: list[str] = []
        for value in self.supported_data_formats or []:
            try:
                labels.append(str(SubmissionDataFormat(value).label))
            except Exception:
                labels.append(str(value))
        return labels


# ── Unified Signal Model ────────────────────────────────────────────
#
# These four models implement the unified signal architecture: a
# single relational model for signal metadata supporting contract
# definition, per-step binding, computed derivations, and runtime
# audit tracing.
# ─────────────────────────────────────────────────────────────────────


class StepIODefinition(TimeStampedModel):
    """The stable data contract for a step input or step output.

    Per ADR-2026-05-22b, "step input" and "step output" are the precise
    terms for step-local catalog entries (whose values live in the
    ``i.*`` and ``o.*`` CEL namespaces respectively). This model was
    formerly named ``SignalDefinition``; the rename in PR 3 aligned
    the code with the vocabulary used in user-facing UI and docs.

    The legacy database table name (``validations_signaldefinition``)
    is preserved via ``Meta.db_table`` so the rename is purely a
    Python-symbol change — no schema-rename migration required.

    A StepIODefinition declares that a validator or workflow step expects
    (input) or produces (output) a named data point with a specific type.
    It is the "what" — the contract — not the "where" (that is the binding).

    This model unifies signal metadata that was previously scattered across
    three legacy storage formats (ValidatorCatalogEntry, FMU config
    JSON, template config JSON) into a single relational table. Every
    feature that touches
    signals — the signals UI, CEL context building, FMU envelope assembly,
    assertion evaluation — works against this one model.

    Key concepts:

    **contract_key vs native_name:** ``contract_key`` is the stable,
    slug-safe identifier used in CEL expressions, the API, and data path
    bindings (e.g., ``panel_area``). ``native_name`` preserves the
    provider's original name verbatim (e.g., an FMU's modelDescription.xml
    variable name ``Panel.Area_m2`` or an EnergyPlus template variable
    ``#{heating_setpoint}``). The contract_key is what Validibot uses;
    the native_name is what the provider uses. They may be identical for
    simple cases.

    **Ownership (XOR constraint):** Each signal is owned by exactly one of:
    - A ``Validator`` — shared signal definitions that apply to every step
      using that validator (library validators).
    - A ``WorkflowStep`` — per-step signal definitions for step-level FMU
      uploads, template scans, or author-customized signals.

    **source_kind:** Declares how the signal's value is obtained.
    ``PAYLOAD_PATH`` means the value comes from a known path in the
    submission payload or metadata (the author may or may not be able
    to change the path — see ``is_path_editable``). ``INTERNAL`` means
    the validator has its own mechanism for extracting or computing the
    value (e.g., EnergyPlus parsing simulation metrics, FMU reading
    output variables). This distinction is surfaced in the UI so
    workflow authors know which signals they can configure.

    **is_path_editable:** Controls whether the workflow author can edit
    the source data path for this signal's ``StepInputBinding``. When
    ``False``, the source path field in the signal edit modal is
    disabled. Typically ``False`` for signals where the validator
    controls the extraction path internally (all EnergyPlus and THERM
    signals, FMU output signals).

    **provider_binding:** A JSON column holding validator-type-specific
    properties that the resolution layer needs but that are not part of the
    generic signal contract. For FMU signals this includes ``causality``,
    ``value_reference``, and ``variability``. For EnergyPlus template
    signals this includes ``variable_type``, ``min``, ``max``, and
    ``choices``. Typed access is provided by Pydantic accessor models.
    """

    contract_key = models.SlugField(
        max_length=255,
        help_text=(
            "Stable slug identifier used in CEL expressions, API responses, "
            "and data path bindings. Must be unique per owner and direction."
        ),
    )
    native_name = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "The provider's original name for this signal, preserved "
            "verbatim (e.g., an FMU variable name or template placeholder)."
        ),
    )
    label = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Human-readable display label for the signal.",
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Detailed description of what this signal represents.",
    )
    direction = models.CharField(
        max_length=10,
        choices=SignalDirection.choices,
        help_text="Whether this signal is consumed (input) or produced (output).",
    )
    data_type = models.CharField(
        max_length=20,
        choices=CatalogValueType.choices,
        default=CatalogValueType.NUMBER,
        help_text="The data type of the signal value.",
    )
    origin_kind = models.CharField(
        max_length=20,
        choices=SignalOriginKind.choices,
        help_text=(
            "How this signal definition was created: "
            "from a validator config declaration, "
            "an FMU probe, or a template scan."
        ),
    )
    source_kind = models.CharField(
        max_length=20,
        choices=SignalSourceKind.choices,
        default=SignalSourceKind.PAYLOAD_PATH,
        help_text=(
            "How the signal's value is obtained: from a known data path "
            "in the submission payload (PAYLOAD_PATH) or via the "
            "validator's own internal extraction mechanism (INTERNAL)."
        ),
    )
    is_path_editable = models.BooleanField(
        default=True,
        help_text=(
            "Whether the workflow author can edit the source data path "
            "for this signal's step binding. False for signals where "
            "the validator controls the extraction path internally."
        ),
    )
    validator = models.ForeignKey(
        "Validator",
        on_delete=models.CASCADE,
        related_name="signal_definitions",
        null=True,
        blank=True,
        help_text=(
            "The validator that owns this signal (for library validators). "
            "Mutually exclusive with workflow_step."
        ),
    )
    workflow_step = models.ForeignKey(
        "workflows.WorkflowStep",
        on_delete=models.CASCADE,
        related_name="signal_definitions",
        null=True,
        blank=True,
        help_text=(
            "The workflow step that owns this signal (for step-level FMUs, "
            "templates, or author-customized signals). "
            "Mutually exclusive with validator."
        ),
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Display ordering within the owner's signal list.",
    )
    is_hidden = models.BooleanField(
        default=False,
        help_text="If True, this signal is hidden from the default signals UI.",
    )
    unit = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Unit of measurement (e.g., 'kW', 'm2', 'degC').",
    )
    provider_binding = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Validator-type-specific properties needed by the resolution "
            "layer (e.g., FMU causality/value_reference, EnergyPlus "
            "variable_type/min/max). Typed access via Pydantic models."
        ),
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary metadata for extensions and integrations.",
    )
    promoted_signal_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=(
            "Optional name for symmetric promotion (per ADR-2026-05-22b). "
            "When set on a step input or step output, the value is "
            "promoted into the s (signal) workflow vocabulary, available "
            "in downstream steps as s.<promoted_signal_name>. Must be a "
            "valid CEL identifier."
        ),
    )

    # Per ADR-2026-05-22: behaviour when this entry's value cannot be
    # resolved at runtime. Mirrors the CatalogEntrySpec.on_missing
    # field so the catalog's intent is persisted to the database row
    # (where it survives sync_validators re-runs). Runtime enforcement
    # is deferred — when implemented, the parser-result merge and CEL
    # context build will consult this field to either error, inject
    # null, or silently omit per the chosen policy.
    on_missing = models.CharField(
        max_length=10,
        default="null",
        choices=[
            ("error", "Fail the run with a clear message"),
            ("null", "Inject null; assertions guard with has() or != null"),
            ("ignore", "Omit silently; references resolve to null"),
        ],
        help_text=(
            "Behaviour when the value cannot be resolved. Default 'null' "
            "is the safe choice; 'error' for entries downstream "
            "assertions reliably depend on; 'ignore' for genuinely "
            "optional facts. Runtime enforcement is deferred — the "
            "field is captured now so future PRs can read intent."
        ),
    )

    class Meta:
        # Preserve the legacy table name from when the model was called
        # SignalDefinition (Phase 2 rename → StepIODefinition).
        # Keeping the table name unchanged means the Python class rename
        # was a zero-migration change; only the RenameField migration
        # for signal_name → promoted_signal_name was unavoidable.
        db_table = "validations_signaldefinition"
        constraints = [
            # Exactly one of validator or workflow_step must be set.
            models.CheckConstraint(
                condition=(
                    Q(validator__isnull=False, workflow_step__isnull=True)
                    | Q(validator__isnull=True, workflow_step__isnull=False)
                ),
                name="ck_sigdef_one_owner",
            ),
            # Unique (validator, contract_key, direction) for validator-owned.
            models.UniqueConstraint(
                fields=["validator", "contract_key", "direction"],
                condition=Q(validator__isnull=False),
                name="uq_sigdef_validator_key_dir",
            ),
            # Unique (workflow_step, contract_key, direction) for step-owned.
            models.UniqueConstraint(
                fields=["workflow_step", "contract_key", "direction"],
                condition=Q(workflow_step__isnull=False),
                name="uq_sigdef_step_key_dir",
            ),
        ]
        ordering = ["order", "pk"]

    # ── Typed metadata accessors ─────────────────────────────

    @property
    def fmu_metadata(self):
        """Typed access to FMU-specific UI/presentation metadata."""
        from validibot.validations.signal_metadata.metadata import FMUSignalMetadata

        return FMUSignalMetadata(**(self.metadata or {}))

    @property
    def fmu_binding(self):
        """Typed access to FMU-specific provider binding."""
        from validibot.validations.signal_metadata.metadata import FMUProviderBinding

        return FMUProviderBinding(**(self.provider_binding or {}))

    @property
    def template_metadata(self):
        """Typed access to template-specific UI/presentation metadata."""
        from validibot.validations.signal_metadata.metadata import (
            TemplateSignalMetadata,
        )

        return TemplateSignalMetadata(**(self.metadata or {}))

    @property
    def energyplus_binding(self):
        """Typed access to EnergyPlus-specific provider binding."""
        from validibot.validations.signal_metadata.metadata import (
            EnergyPlusProviderBinding,
        )

        return EnergyPlusProviderBinding(**(self.provider_binding or {}))

    def __str__(self):
        owner = self.validator or self.workflow_step
        return f"{owner}:{self.contract_key} ({self.direction})"


class StepInputBinding(TimeStampedModel):
    """Per-step wiring that maps a step input to a data source.

    While ``StepIODefinition`` declares *what* data a step expects,
    ``StepInputBinding`` declares *where* to find it. This is the
    per-step mapping layer that connects each step input to a
    concrete location in the submission payload, submission metadata,
    a workflow signal, an upstream step's output, or a system value.

    Per ADR-2026-05-22b, this model was formerly named
    ``StepSignalBinding``; the rename in PR 3 reflects the fact that
    bindings only ever apply to step INPUTS (outputs are produced by
    the validator, not bound to anything). The legacy database table
    name (``validations_stepsignalbinding``) is preserved via
    ``Meta.db_table`` so no schema-rename migration is needed.

    This separation allows the same step input definition (e.g.,
    ``panel_area``) to be wired differently in different workflow
    steps — one step might read it from ``building.envelope.panel_area``
    in the submission JSON, while another reads it from a workflow
    signal or upstream step output.

    Key fields:

    - ``source_scope``: Where to look for the value (submission payload,
      submission metadata, workflow signal, upstream step output, or system).
    - ``source_data_path``: A dotted path expression (e.g.,
      ``weather.stations[0].solar_irradiance``) into the source scope.
    - ``default_value``: Fallback value when the source path resolves to
      nothing and the input is not required.
    - ``is_required``: If True, a missing value with no default raises a
      structured error before validator execution.
    """

    workflow_step = models.ForeignKey(
        "workflows.WorkflowStep",
        on_delete=models.CASCADE,
        related_name="signal_bindings",
        help_text="The workflow step this binding belongs to.",
    )
    signal_definition = models.ForeignKey(
        StepIODefinition,
        on_delete=models.CASCADE,
        related_name="bindings",
        help_text="The step input definition this binding wires up.",
    )
    source_scope = models.CharField(
        max_length=30,
        choices=BindingSourceScope.choices,
        default=BindingSourceScope.SUBMISSION_PAYLOAD,
        help_text=(
            "The data scope to resolve the input value from: submission "
            "payload, submission metadata, workflow signal, upstream step "
            "output, or system."
        ),
    )
    source_data_path = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "Path expression into the source scope. Supports dot "
            "notation (e.g., 'building.panel_area'), [index] for "
            "arrays, and filter expressions for named elements "
            "(e.g., 'items[?@.name==\"x\"].value')."
        ),
    )
    default_value = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text=(
            "Fallback value used when the source path resolves to nothing "
            "and the input is not marked as required."
        ),
    )
    is_required = models.BooleanField(
        default=True,
        help_text=(
            "If True, a missing value with no default raises a structured "
            "error before validator execution begins."
        ),
    )

    class Meta:
        # Preserve the legacy table name from when this model was called
        # StepInputBinding. Renaming the Python class without renaming
        # the database table makes this purely a code-level rename — no
        # schema migration required for the class rename.
        db_table = "validations_stepsignalbinding"
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_step", "signal_definition"],
                name="uq_binding_step_signal",
            ),
        ]

    def __str__(self):
        return (
            f"{self.workflow_step}:{self.signal_definition.contract_key} "
            f"← {self.source_scope}:{self.source_data_path}"
        )


class WorkflowStepIOPromotion(TimeStampedModel):
    """Workflow-scoped overlay for promoting a **validator-owned** step
    input/output to ``s.*``.

    A ``StepIODefinition`` that's owned by a ``Validator`` (e.g. an
    EnergyPlus catalog row) is shared across every workflow that uses
    that validator. The in-row ``promoted_signal_name`` field can't
    carry a workflow-scoped name because a single value would have to
    serve every workflow. This overlay model decouples the promotion
    (workflow-scoped, per workflow_step) from the catalog row
    (validator-scoped, shared).

    Two storage paths for the same logical concept, split by row
    ownership:

    - **Step-owned** ``StepIODefinition`` rows: the in-row
      ``promoted_signal_name`` field holds the name. There is one
      owner, so no scope ambiguity — and ``WorkflowStepIOPromotion``
      rows pointing at a step-owned ``signal_definition`` are
      **forbidden** by ``clean()``. Without that prohibition, runtime
      would inject the same value under two ``s.*`` aliases (one from
      the in-row scan, one from the overlay scan).
    - **Validator-owned** ``StepIODefinition`` rows: the overlay
      carries the name, keyed on ``(workflow_step,
      signal_definition)``.

    Author-facing terminology stays the same ("Copy to Signal", "step
    output", "promoted to s.…"). The split is purely an implementation
    detail that solves the workflow-scope problem without renaming
    fields or breaking step-owned promotions that already work.

    Added in response to the May 2026 code review's P1 finding —
    before this model existed, Copy-to-Signal on EnergyPlus catalog
    rows 404'd because ``WorkflowStepPromoteStepIOView`` required the
    ``StepIODefinition`` to have a non-null ``workflow_step`` FK. The
    "validator-owned only" invariant was added in a follow-up review
    after the runtime "two truths" risk was identified.
    """

    workflow_step = models.ForeignKey(
        "workflows.WorkflowStep",
        on_delete=models.CASCADE,
        related_name="io_promotions",
        help_text=(
            "The workflow step that owns this promotion. "
            "Same step the assertion form is editing — the overlay "
            "is workflow-scoped, not validator-scoped."
        ),
    )
    signal_definition = models.ForeignKey(
        StepIODefinition,
        on_delete=models.CASCADE,
        related_name="step_promotions",
        help_text=(
            "The validator-owned step input or step output being "
            "promoted. Step-owned rows must use their in-row "
            "promoted_signal_name field instead — overlay rows on "
            "step-owned definitions are rejected by clean()."
        ),
    )
    promoted_signal_name = models.CharField(
        max_length=100,
        help_text=(
            "The workflow-vocabulary name the promoted value is "
            "exposed under (s.<promoted_signal_name>). Must be a "
            "valid CEL identifier and unique across the workflow."
        ),
    )

    class Meta:
        constraints = [
            # Only one overlay per (step, signal_definition) — the
            # author can't promote the same catalog row twice within
            # the same step under different names.
            models.UniqueConstraint(
                fields=["workflow_step", "signal_definition"],
                name="uq_iopromotion_step_signal",
            ),
            # Only one overlay per (step, promoted_signal_name) — two
            # different catalog rows can't both promote to the same
            # ``s.<name>`` within the same step. Workflow-wide
            # uniqueness (across all steps + workflow signal
            # mappings) stays application-level in
            # ``validate_signal_name_unique`` because enforcing it at
            # the DB layer would require a denormalized workflow FK
            # on this row. This per-step constraint protects against
            # race conditions and manual ORM writes that bypass the
            # form's uniqueness check for the most common collision
            # shape (two overlays on the same step).
            models.UniqueConstraint(
                fields=["workflow_step", "promoted_signal_name"],
                name="uq_iopromotion_step_name",
            ),
        ]

    def clean(self):
        """Enforce overlay invariants.

        Two invariants protect ``s.*`` injection from inconsistency:

        1. **Validator-owned only.** Step-owned StepIODefinitions
           must use their in-row ``promoted_signal_name`` field
           instead. Runtime injection scans BOTH sources (in-row +
           overlay), so a step-owned row with both would be injected
           twice — potentially under different aliases.

        2. **Same-validator scope.** The signal_definition's
           ``validator`` FK must match the workflow_step's
           ``validator`` FK. Otherwise an overlay could attach an
           EnergyPlus catalog row to an unrelated FMU step, with
           runtime then trying to read a non-existent contract_key
           from that step's output. The promote view at
           ``WorkflowStepPromoteStepIOView`` already enforces this
           for HTTP writes; mirroring it here protects service-
           layer and migration writes too.

        Both rules are application-level (via ``clean()`` +
        ``full_clean()`` from ``save()``) to match how the rest of
        the contract layer enforces XOR ownership on
        ``StepIODefinition``. Direct SQL writes can still bypass
        them — the prohibition communicates intent and stops honest
        callers from drifting.
        """
        super().clean()
        if not self.signal_definition_id:
            return

        from django.core.exceptions import ValidationError

        # Invariant 1: validator-owned only.
        if self.signal_definition.workflow_step_id is not None:
            raise ValidationError(
                {
                    "signal_definition": (
                        "WorkflowStepIOPromotion can only overlay "
                        "validator-owned StepIODefinitions. Step-owned "
                        "rows must use their in-row "
                        "promoted_signal_name field instead — otherwise "
                        "runtime would inject the value twice."
                    ),
                },
            )

        # Invariant 2: same-validator scope. The signal_definition
        # must belong to the same validator the workflow_step is
        # bound to. Without this, the promote view's existing check
        # is the only thing keeping cross-validator overlays out;
        # service-layer writes could create them silently.
        #
        # Per Invariant 1 above, ``sig_validator_id`` is guaranteed
        # non-null at this point: the StepIODefinition XOR
        # constraint requires exactly one of ``validator`` /
        # ``workflow_step`` to be set, and Invariant 1 rejects any
        # ``signal_definition`` whose ``workflow_step`` IS set.
        #
        # The remaining risk is ``step_validator_id is None`` —
        # which would be a BASIC/ruleset step that doesn't bind to
        # a validator at all. An overlay in that shape makes no
        # sense: the catalog row's contract_key has no runtime path
        # to s.* because the step never invokes the validator that
        # would emit it. We reject that case explicitly rather than
        # silently allowing the overlay to be written and quietly
        # never fire at runtime.
        step_validator_id = getattr(self.workflow_step, "validator_id", None)
        sig_validator_id = self.signal_definition.validator_id
        if step_validator_id is None or step_validator_id != sig_validator_id:
            raise ValidationError(
                {
                    "signal_definition": (
                        "WorkflowStepIOPromotion.signal_definition "
                        "must belong to the same validator as the "
                        "workflow_step (and the step must have a "
                        "validator). Attaching a catalog row from "
                        "one validator to a step bound to a "
                        "different validator — or to a step with no "
                        "validator at all — would produce a "
                        "contract_key that the step's runtime never "
                        "emits."
                    ),
                },
            )

    def save(self, *args, **kwargs):
        # Run clean() before save so the validator-owned-only rule is
        # enforced for ORM writes that don't go through a Form's
        # full_clean() (e.g. service-layer code and migrations).
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.workflow_step}:{self.signal_definition.contract_key} "
            f"→ s.{self.promoted_signal_name}"
        )


class Derivation(TimeStampedModel):
    """A computed value defined by a CEL expression over signals.

    Derivations are named, typed values that are computed from input
    signals and other derivations using CEL (Common Expression Language)
    expressions. They represent a distinct concept from signals: signals
    are data points that flow in or out of a validator; derivations are
    intermediate computed values used in assertions and reporting.

    Like ``StepIODefinition``, each derivation is owned by exactly one of
    a ``Validator`` (shared across all steps using that validator) or a
    ``WorkflowStep`` (per-step customization). The same XOR ownership
    constraint applies.

    The ``expression`` field contains a CEL expression that can reference
    signal contract_keys and other derivation contract_keys by name.
    The ``data_type`` is limited to scalar types (number, string, boolean)
    since derivations produce single computed values, not complex objects
    or timeseries.
    """

    contract_key = models.SlugField(
        max_length=255,
        help_text=(
            "Stable slug identifier for this derivation, used in CEL "
            "expressions and API responses."
        ),
    )
    expression = models.TextField(
        help_text=(
            "CEL expression that computes this derivation's value. Can "
            "reference signal contract_keys and other derivation keys."
        ),
    )
    description = models.TextField(
        blank=True,
        default="",
        help_text="Detailed description of what this derivation computes.",
    )
    label = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Human-readable display label for the derivation.",
    )
    data_type = models.CharField(
        max_length=20,
        choices=[
            (CatalogValueType.NUMBER, CatalogValueType.NUMBER.label),
            (CatalogValueType.STRING, CatalogValueType.STRING.label),
            (CatalogValueType.BOOLEAN, CatalogValueType.BOOLEAN.label),
        ],
        default=CatalogValueType.NUMBER,
        help_text=(
            "The data type of the computed value. Limited to scalar types "
            "(number, string, boolean)."
        ),
    )
    validator = models.ForeignKey(
        "Validator",
        on_delete=models.CASCADE,
        related_name="derivations",
        null=True,
        blank=True,
        help_text=(
            "The validator that owns this derivation (for library validators). "
            "Mutually exclusive with workflow_step."
        ),
    )
    workflow_step = models.ForeignKey(
        "workflows.WorkflowStep",
        on_delete=models.CASCADE,
        related_name="derivations",
        null=True,
        blank=True,
        help_text=(
            "The workflow step that owns this derivation. "
            "Mutually exclusive with validator."
        ),
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Display ordering within the owner's derivation list.",
    )
    is_hidden = models.BooleanField(
        default=False,
        help_text="If True, this derivation is hidden from the default UI.",
    )
    unit = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Unit of measurement for the computed value.",
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary metadata for extensions and integrations.",
    )

    class Meta:
        constraints = [
            # Exactly one of validator or workflow_step must be set.
            models.CheckConstraint(
                condition=(
                    Q(validator__isnull=False, workflow_step__isnull=True)
                    | Q(validator__isnull=True, workflow_step__isnull=False)
                ),
                name="ck_derivation_one_owner",
            ),
            # Unique (validator, contract_key) for validator-owned.
            models.UniqueConstraint(
                fields=["validator", "contract_key"],
                condition=Q(validator__isnull=False),
                name="uq_derivation_validator_key",
            ),
            # Unique (workflow_step, contract_key) for step-owned.
            models.UniqueConstraint(
                fields=["workflow_step", "contract_key"],
                condition=Q(workflow_step__isnull=False),
                name="uq_derivation_step_key",
            ),
        ]
        ordering = ["order", "pk"]

    def __str__(self):
        owner = self.validator or self.workflow_step
        return f"{owner}:{self.contract_key}"


class ResolvedInputTrace(TimeStampedModel):
    """Runtime audit record of how an input signal was resolved.

    Each time the resolution engine processes a step run's input signals,
    it creates one ``ResolvedInputTrace`` per input signal. This provides
    a complete audit trail of:

    - Which signal was being resolved (``signal_definition`` FK and
      denormalized ``signal_contract_key``).
    - Which source scope and data path were used to find the value.
    - Whether resolution succeeded (``resolved``), fell back to a default
      (``used_default``), or failed (``error_message``).
    - A snapshot of the resolved value for debugging and reproducibility.

    The ``signal_contract_key`` is denormalized from the signal definition
    so that traces remain meaningful even if the signal definition is later
    deleted (the FK is SET_NULL). This supports long-term auditability
    without preventing schema evolution.

    The ``upstream_step_key`` is populated when ``source_scope_used`` is
    ``upstream_step``, recording which step's output was consulted.
    """

    step_run = models.ForeignKey(
        "ValidationStepRun",
        on_delete=models.CASCADE,
        related_name="input_traces",
        help_text="The step run this trace belongs to.",
    )
    signal_definition = models.ForeignKey(
        StepIODefinition,
        on_delete=models.SET_NULL,
        null=True,
        help_text=(
            "The signal definition that was being resolved. SET_NULL on "
            "delete so traces survive schema evolution."
        ),
    )
    signal_contract_key = models.CharField(
        max_length=255,
        help_text=(
            "Denormalized contract_key from the signal definition, "
            "preserved for auditability if the definition is deleted."
        ),
    )
    source_scope_used = models.CharField(
        max_length=30,
        help_text=(
            "The source scope that was actually used to resolve the value "
            "(may differ from the binding's configured scope if fallback "
            "logic was applied)."
        ),
    )
    source_data_path_used = models.CharField(
        max_length=500,
        help_text=("The data path that was actually used to resolve the value."),
    )
    upstream_step_key = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=(
            "If source_scope_used is 'upstream_step', the key of the "
            "upstream step whose output was consulted."
        ),
    )
    resolved = models.BooleanField(
        help_text="Whether the resolution engine found a value for this signal.",
    )
    used_default = models.BooleanField(
        default=False,
        help_text=(
            "Whether the resolved value came from the binding's default_value "
            "rather than the source data path."
        ),
    )
    value_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text=(
            "Snapshot of the resolved value at resolution time, for "
            "debugging and reproducibility."
        ),
    )
    error_message = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Error message if resolution failed (e.g., required signal "
            "not found and no default configured)."
        ),
    )

    def __str__(self):
        status = "resolved" if self.resolved else "FAILED"
        return f"{self.signal_contract_key} → {status}"


class CustomValidator(TimeStampedModel):
    """
    Author-defined validator built on top of a base validation type.
    """

    validator = models.OneToOneField(
        Validator,
        on_delete=models.CASCADE,
        related_name="custom_validator",
    )
    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="custom_validator_configs",
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="custom_validators",
    )
    custom_type = models.CharField(
        max_length=32,
        choices=CustomValidatorType.choices,
    )
    base_validation_type = models.CharField(
        max_length=80,
    )
    notes = models.TextField(
        blank=True,
        default="",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["org", "custom_type", "validator"],
                name="uq_custom_validator_org_type_validator",
            ),
        ]

    def clean(self):
        super().clean()
        if self.validator.validation_type != self.base_validation_type:
            raise ValidationError(
                {
                    "base_validation_type": _(
                        "Base validation type must match the linked "
                        "Validator validation_type.",
                    ),
                },
            )
        if self.validator.org_id and self.validator.org_id != self.org_id:
            raise ValidationError(
                {
                    "validator": _(
                        "Validator already belongs to a different organization.",
                    ),
                },
            )

    def save(self, *args, **kwargs):
        # Ensure the linked validator points at this org and is marked custom.
        self.validator.org = self.org
        self.validator.is_system = False
        if not self.validator.slug:
            self.validator.slug = slugify(f"{self.org_id}-{self.validator.name}")
        self.validator.save()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.org} · {self.custom_type}"


class ValidationRun(TimeStampedModel):
    """
    One execution of a Submission through a specific Workflow version.

    Not normalized, as Workflow has a link to org and user,
    but we store org/project/user here to preserve historical truth,
    query performance and access control.

    """

    class Meta:
        indexes = [
            models.Index(fields=["org", "project", "workflow", "created"]),
            models.Index(fields=["status", "created"]),
        ]
        constraints = [
            # ended_at cannot be before started_at (allow nulls)
            models.CheckConstraint(
                name="ck_run_times_valid",
                condition=Q(ended_at__isnull=True)
                | Q(started_at__isnull=True)
                | Q(ended_at__gte=models.F("started_at")),
            ),
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="validation_runs",
    )

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.PROTECT,
        related_name="validation_runs",
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="validation_runs",
    )

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="validation_runs",
    )

    submission = models.ForeignKey(
        Submission,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="runs",
        help_text=_(
            "Can be NULL if submission record was deleted. "
            "Code accessing this field must handle the None case."
        ),
    )

    status = models.CharField(
        max_length=16,
        choices=ValidationRunStatus.choices,
        default=ValidationRunStatus.PENDING,
    )

    short_description = models.CharField(
        max_length=VALIDATION_RUN_SHORT_DESCRIPTION_MAX_LENGTH,
        blank=True,
        default="",
        help_text=_("Optional short description of this validation run."),
    )

    started_at = models.DateTimeField(null=True, blank=True)

    ended_at = models.DateTimeField(null=True, blank=True)

    duration_ms = models.BigIntegerField(default=0)

    summary = models.JSONField(default=dict, blank=True)  # counts, pass/fail, etc.

    error = models.TextField(
        blank=True,
        default="",
    )  # terminal error/trace if any

    error_category = models.CharField(
        max_length=32,
        choices=ValidationRunErrorCategory.choices,
        blank=True,
        default="",
        help_text=_("Classification of why the run failed (TIMEOUT, OOM, etc.)."),
    )

    source = models.CharField(
        max_length=32,
        choices=ValidationRunSource.choices,
        default=ValidationRunSource.LAUNCH_PAGE,
        help_text=_("Where this run was initiated (web launch page, API, etc.)."),
    )

    # Output retention fields
    # ~---------------------------------------------------------------
    # These track when validator outputs (results, artifacts, findings) should
    # be purged. The retention policy is snapshotted from the workflow at
    # run creation time.

    output_retention_policy = models.CharField(
        max_length=32,
        choices=OutputRetention.choices,
        default=OutputRetention.STORE_30_DAYS,
        help_text=_("Snapshot of workflow's output retention policy at run time."),
    )

    output_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "When outputs should be purged (null = never expires or not yet computed)."
        ),
    )

    output_purged_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When outputs were purged (for audit trail)."),
    )

    output_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "SHA-256 hash of the canonical validation output record, computed "
            "at run completion. Covers the stable workflow context and the "
            "final run outcome."
        ),
    )

    def clean(self):
        super().clean()
        # Optional but helpful: ensure org consistency with submission
        if self.submission_id and self.org_id and self.submission.org_id != self.org_id:
            raise ValidationError({"org": _("Run org must match submission org.")})

    @property
    def status_pill_class(self) -> str:
        return {
            ValidationRunStatus.PENDING: "bg-secondary",
            ValidationRunStatus.RUNNING: "bg-primary",
            ValidationRunStatus.SUCCEEDED: "bg-success",
            ValidationRunStatus.FAILED: "bg-danger",
            ValidationRunStatus.CANCELED: "bg-warning text-dark",
        }.get(self.status, "bg-secondary")

    @property
    def computed_duration_ms(self) -> int | None:
        if self.duration_ms:
            return int(self.duration_ms)
        if not self.started_at or not self.ended_at:
            return None
        delta = self.ended_at - self.started_at
        return max(int(delta.total_seconds() * 1000), 0)

    @property
    def current_step_run(self) -> ValidationStepRun | None:
        """
        Returns the currently active ValidationStepRun for this run.

        Active means status is PENDING or RUNNING. If multiple steps are active
        (which shouldn't happen), returns the first one by step_order.
        """
        from validibot.validations.constants import StepStatus

        return (
            self.step_runs.filter(
                status__in=[StepStatus.PENDING, StepStatus.RUNNING],
            )
            .order_by("step_order")
            .first()
        )

    @property
    def user_friendly_error(self) -> str:
        """
        Returns a human-friendly error message based on error_category.

        Falls back to the raw error text if no category is set, or empty
        string if there's no error at all.
        """
        from validibot.validations.constants import VALIDATION_RUN_ERROR_MESSAGES

        if self.error_category:
            return VALIDATION_RUN_ERROR_MESSAGES.get(
                self.error_category,
                self.error or "",
            )
        return self.error or ""


class ValidationRunSummary(TimeStampedModel):
    """
    Durable aggregate snapshot of a ValidationRun.

    Retains severity totals and per-step metadata even after findings are purged.
    """

    class Meta:
        ordering = ["-created"]

    run = models.OneToOneField(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="summary_record",
        primary_key=True,
    )

    status = models.CharField(
        max_length=16,
        choices=ValidationRunStatus.choices,
        default=ValidationRunStatus.PENDING,
    )

    completed_at = models.DateTimeField(null=True, blank=True)

    total_findings = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    info_count = models.PositiveIntegerField(default=0)

    assertion_failure_count = models.PositiveIntegerField(default=0)
    assertion_total_count = models.PositiveIntegerField(default=0)

    extras = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Optional metadata for reporting (for example exemplar messages)."),
    )

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"Summary for run {self.run_id} ({self.status})"


class ValidationStepRun(TimeStampedModel):
    """
    Execution of a single WorkflowStep within a ValidationRun.

    Results of the step run are stored as ValidationFindings linked to this model.

    """

    class Meta:
        indexes = [
            models.Index(fields=["validation_run", "status"]),
        ]
        constraints = [
            # Prefer UniqueConstraint to future-proof
            models.UniqueConstraint(
                fields=[
                    "validation_run",
                    "step_order",
                ],
                name="uq_step_run_run_order",
            ),
            # Prevent duplicate execution rows for same step in
            # same run (optional but recommended)
            models.UniqueConstraint(
                fields=[
                    "validation_run",
                    "workflow_step",
                ],
                name="uq_step_run_run_step",
            ),
            models.CheckConstraint(
                name="ck_step_run_times_valid",
                condition=Q(ended_at__isnull=True)
                | Q(started_at__isnull=True)
                | Q(ended_at__gte=models.F("started_at")),
            ),
        ]

    validation_run = models.ForeignKey(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="step_runs",
    )

    workflow_step = models.ForeignKey(
        WorkflowStep,
        on_delete=models.PROTECT,
        related_name="+",
    )

    step_order = (
        models.PositiveIntegerField()
    )  # denormalized copy of step.order for quick lookup

    status = models.CharField(
        max_length=16,
        choices=StepStatus.choices,
        default=StepStatus.PENDING,
    )

    started_at = models.DateTimeField(null=True, blank=True)

    ended_at = models.DateTimeField(null=True, blank=True)

    duration_ms = models.BigIntegerField(default=0)

    output = models.JSONField(
        default=dict,
        blank=True,
    )  # machine output (validator-specific)

    error = models.TextField(blank=True, default="")

    # Trust ADR Phase 5 Session A — content-addressed identifier for
    # the validator backend container image that ran this step.
    # Populated by the runner from Docker's RepoDigests (preferred,
    # registry-anchored) or local image ID (fallback for dev images
    # not pulled from a registry). Empty string when there's no
    # backend (simple-validator steps run inline in the Django
    # process, so they have no container to digest). Stored as a
    # dedicated column rather than a key in ``output`` JSON because
    # it's part of the universal trust contract — auditors query it
    # for coverage gaps independently of validator-specific output.
    validator_backend_image_digest = models.CharField(
        max_length=256,
        blank=True,
        default="",
        help_text=(
            "Resolved sha256 digest of the validator backend image that "
            "executed this step (e.g. 'sha256:abc...' or "
            "'registry/path/...@sha256:abc...'). Empty for "
            "simple-validator steps that run inline without a container. "
            "256 chars accommodates registry-anchored references "
            "(registry path + image name + '@sha256:' + 64 hex), which "
            "commonly run 100+ chars for nested GCS / GAR paths."
        ),
    )

    def __str__(self) -> str:
        if self.workflow_step:
            name = (
                f"Results from Step {self.workflow_step.step_number} :"
                f"{self.workflow_step.name}"
            )
            return name
        return super().__str__()

    def clean(self):
        super().clean()

        if (
            self.workflow_step
            and self.validation_run
            and self.workflow_step.workflow_id != self.validation_run.workflow_id
        ):
            raise ValidationError(
                {
                    "workflow_step": _("Step must belong to the run's workflow."),
                },
            )

        if (
            self.workflow_step
            and self.step_order
            and self.workflow_step.order != self.step_order
        ):
            raise ValidationError({"step_order": _("Must equal WorkflowStep.order.")})


class ValidationStepRunSummary(TimeStampedModel):
    """
    Lightweight per-step snapshot tied to ValidationRunSummary.
    """

    class Meta:
        ordering = ["step_order", "id"]
        indexes = [
            models.Index(fields=["summary", "status"]),
        ]

    summary = models.ForeignKey(
        ValidationRunSummary,
        on_delete=models.CASCADE,
        related_name="step_summaries",
    )

    step_run = models.OneToOneField(
        ValidationStepRun,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="step_summary",
    )

    step_name = models.CharField(max_length=255, blank=True, default="")
    step_order = models.PositiveIntegerField(default=0)

    status = models.CharField(
        max_length=16,
        choices=StepStatus.choices,
        default=StepStatus.PENDING,
    )

    error_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    info_count = models.PositiveIntegerField(default=0)


class ValidationFinding(TimeStampedModel):
    """
    Normalized issue emitted during a validation step.

    A ValidationFinding is the durable, queryable representation of a single
    validator message (error/warning/info) produced while executing a
    ValidationStepRun. Findings are stored separately from validator-specific
    `ValidationStepRun.output` so the UI/API can filter, sort, and paginate
    issues efficiently.

    Data model relationships:
    - `validation_step_run` is the primary parent for the finding.
    - `validation_run` is denormalized for performance (kept aligned by
      `_ensure_run_alignment`).
    - `ruleset_assertion` optionally links the finding back to a configured
      RulesetAssertion when the validator can attribute the issue to an assertion.

    Aggregates such as ValidationRunSummary and ValidationStepRunSummary are
    computed from this table (severity totals, per-step counts).

    The `path` field records a location within the submitted artifact (JSON
    Pointer, XPath, etc.). For JSON submissions we strip the synthetic `payload`
    prefix to match user-facing paths (see `_strip_payload_prefix`).
    """

    class Meta:
        indexes = [
            models.Index(fields=["validation_run", "severity"]),
            models.Index(fields=["validation_step_run", "severity"]),
            models.Index(fields=["validation_step_run", "code"]),
        ]
        ordering = [
            models.Case(
                models.When(severity=Severity.ERROR, then=Value(0)),
                models.When(severity=Severity.WARNING, then=Value(1)),
                default=Value(2),
                output_field=models.IntegerField(),
            ),
            "-created",
        ]

    # We keep a link to both the run and the step run for easier querying.
    # This is a bit of denormalization but improves performance.
    # (Theroetically we could get the run via step_run.validation_run.)
    # To mitigate any issues we define _ensure_run_alignment below and
    # call it in clean().

    validation_run = models.ForeignKey(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="findings",
    )

    validation_step_run = models.ForeignKey(
        ValidationStepRun,
        on_delete=models.CASCADE,
        related_name="findings",
    )

    ruleset_assertion = models.ForeignKey(
        "validations.RulesetAssertion",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="findings",
    )

    severity = models.CharField(max_length=16, choices=Severity.choices)

    code = models.CharField(
        max_length=64,
        blank=True,
        default="",
    )  # e.g. "json.schema.required"

    message = models.TextField()

    path = models.CharField(
        max_length=512,
        blank=True,
        default="",
    )  # JSON Pointer/XPath/etc.

    meta = models.JSONField(default=dict, blank=True)

    def _ensure_run_alignment(self) -> None:
        """
        Guarantee validation_run mirrors the parent run from validation_step_run.
        """

        if not self.validation_step_run_id:
            return

        step_run = self.validation_step_run
        if not step_run:
            return

        parent_run_id = step_run.validation_run_id
        if not parent_run_id:
            return

        if not self.validation_run_id:
            self.validation_run_id = parent_run_id
            return

        if self.validation_run_id != parent_run_id:
            raise ValidationError(
                {
                    "validation_run": _(
                        "Validation run must match the step run's parent run.",
                    ),
                },
            )

    def _strip_payload_prefix(self) -> None:
        """Remove the synthetic 'payload' prefix for JSON submissions."""

        path = (self.path or "").strip()
        if not path:
            return
        run = getattr(self, "validation_run", None)
        submission = getattr(run, "submission", None)
        if not submission or submission.file_type != SubmissionFileType.JSON:
            return
        lower = path.lower()
        prefix = "payload"
        if not lower.startswith(prefix):
            return
        remainder = path[len(prefix) :]
        if remainder and remainder[0] not in {".", "/", "["}:
            return
        remainder = remainder.lstrip("./")
        while remainder.startswith("["):
            remainder = remainder[1:]
        self.path = remainder

    def clean(self):
        super().clean()
        self._ensure_run_alignment()
        self._strip_payload_prefix()

    def save(self, *args, **kwargs):
        self._ensure_run_alignment()
        self._strip_payload_prefix()
        super().save(*args, **kwargs)


def artifact_upload_to(instance, filename: str) -> str:
    f = (
        f"artifacts/org-{instance.org_id}/runs/{instance.run_id}/"
        f"{uuid.uuid4().hex}/{filename}"
    )
    return f


class Artifact(TimeStampedModel):
    """
    Files emitted during a run (logs, reports, transformed docs, E+ outputs).
    """

    class Meta:
        indexes = [models.Index(fields=["validation_run", "created"])]

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )

    validation_run = models.ForeignKey(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )

    label = models.CharField(max_length=120)

    content_type = models.CharField(max_length=128, blank=True, default="")

    file = models.FileField(upload_to=artifact_upload_to)

    size_bytes = models.BigIntegerField(default=0)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.label} (run {self.validation_run_id})"

    def clean(self):
        super().clean()
        if self.org_id and self.run_id and self.org_id != self.run.org_id:
            raise ValidationError({"org": _("Artifact org must match run org.")})


class CallbackReceipt(models.Model):
    """
    Tracks processed container job callbacks for idempotency.

    When a container job called in an async manner (Cloud Run, AWS Batch)
    completes, it POSTs a callback to the worker service. The task queue may
    retry this callback if the initial delivery fails or times out, which
    could cause duplicate processing (duplicate findings, incorrect status).

    This model records each successfully processed callback. Before processing
    a callback, the handler checks if a receipt exists for the callback_id:
    - If found: return 200 OK immediately (already processed)
    - If not found: process the callback and create a receipt

    The callback_id is generated at job launch time and embedded in the input
    envelope. The container job echoes it back in the callback payload.

    Receipts are retained for debugging/audit purposes. A cleanup job can
    delete old receipts after a retention period (e.g., 30 days).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    callback_id = models.CharField(
        max_length=255,
        unique=True,
        help_text=_("Unique callback identifier from the input envelope."),
    )

    validation_run = models.ForeignKey(
        ValidationRun,
        on_delete=models.CASCADE,
        related_name="callback_receipts",
        help_text=_("The validation run this callback was for."),
    )

    received_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When this callback was first processed."),
    )

    # Store minimal callback data for debugging and processing state
    status = models.CharField(
        max_length=50,
        choices=CallbackReceiptStatus.choices,
        default=CallbackReceiptStatus.PROCESSING,
        help_text=_("Processing status of this callback receipt."),
    )

    result_uri = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=_("URI to the output envelope (e.g., gs:// for GCS, s3:// for S3)."),
    )

    class Meta:
        indexes = [
            models.Index(fields=["callback_id"]),
            models.Index(fields=["validation_run"]),
            models.Index(fields=["received_at"]),
        ]

    def __str__(self):
        short_id = self.callback_id[:8]
        return f"CallbackReceipt({short_id}... for run {self.validation_run_id})"


class RunEvidenceArtifactAvailability(models.TextChoices):
    """Lifecycle states for an evidence-manifest artifact.

    ADR-2026-04-27 Phase 4 Session A. Tracks whether the manifest's
    bytes are currently retrievable from storage. The state is
    decoupled from "was the manifest ever generated?" because runs
    can have manifests that get purged later (retention sweeps,
    storage backend rotations).
    """

    GENERATED = "GENERATED", _("Manifest generated and bytes available")
    PURGED = "PURGED", _("Manifest bytes purged; only hash remains")
    FAILED = "FAILED", _("Manifest generation failed; row records the gap")


def _evidence_manifest_upload_path(instance, filename):
    """Generate the storage path for an evidence manifest.

    Format: ``evidence/<org_id>/<run_id>/<filename>``. Per-org
    partition mirrors the run-workspace layout introduced in Phase 1
    so future bulk-delete-by-org operations don't have to walk every
    run dir individually.
    """
    return f"evidence/{instance.run.org_id}/{instance.run_id}/{filename}"


class RunEvidenceArtifact(TimeStampedModel):
    """Per-run evidence-manifest record.

    ADR-2026-04-27 Phase 4 Session A: every completed validation run
    gets exactly one ``RunEvidenceArtifact`` row pointing at the
    canonical-JSON manifest serialised to storage. The row is the
    DB index — manifest bytes live in storage, ``manifest_hash``
    pins them.

    Session B will use this row to record retention-class state and
    Session C will use ``cached_bundle_path`` to point at exported
    tarballs (the manifest plus optional inputs / outputs / signed
    credential).

    Why one-to-one with ``ValidationRun``
    -------------------------------------

    A run has one canonical manifest. Re-running the manifest builder
    (e.g. after a backend bug fix that changed the redaction policy)
    produces a *different* manifest with a different hash; that's a
    new artifact for a new audit row, not an update to this one. We
    do NOT track manifest history here — re-emission is a Phase 4
    Session B / C concern.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    run = models.OneToOneField(
        "validations.ValidationRun",
        on_delete=models.CASCADE,
        related_name="evidence_artifact",
        help_text=_("The validation run this manifest documents."),
    )

    schema_version = models.CharField(
        max_length=64,
        help_text=_(
            "The validibot.evidence.vN schema string the manifest was "
            "produced under. Verifiers parse the manifest's "
            "schema_version field at read time, but storing it here "
            "lets the auditor query for runs on stale schemas without "
            "fetching the bytes."
        ),
    )

    manifest_path = models.FileField(
        upload_to=_evidence_manifest_upload_path,
        max_length=500,
        blank=True,
        help_text=_(
            "Path to the canonical-JSON manifest in default storage. "
            "Empty when availability is FAILED (generation never "
            "produced bytes)."
        ),
    )

    manifest_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "SHA-256 of the manifest's canonical-JSON bytes. Empty "
            "when availability is FAILED. Future signed credentials "
            "(Phase 4 Session C) cite this hash, so a re-fetch + "
            "re-hash detects tampering."
        ),
    )

    cached_bundle_path = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=_(
            "Phase 4 Session C: path to a cached export-bundle tarball "
            "(manifest + inputs + outputs + signature). Empty until "
            "the operator triggers an export."
        ),
    )

    retention_class = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text=_(
            "Workflow.input_retention at the time the manifest was "
            "generated. Mirrored here so retention-sweep code can "
            "filter without joining to Workflow."
        ),
    )

    availability = models.CharField(
        max_length=16,
        choices=RunEvidenceArtifactAvailability,
        default=RunEvidenceArtifactAvailability.GENERATED,
        help_text=_(
            "Lifecycle state: GENERATED (bytes in storage), PURGED "
            "(only hash remains), or FAILED (generation aborted)."
        ),
    )

    generation_error = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "When availability is FAILED, the exception message from "
            "the manifest builder. Empty otherwise."
        ),
    )

    class Meta:
        indexes = [
            models.Index(fields=["availability", "created"]),
            models.Index(fields=["schema_version"]),
        ]

    def __str__(self):
        return (
            f"RunEvidenceArtifact(run={self.run_id}, availability={self.availability})"
        )


def _resource_file_upload_path(instance: ValidatorResourceFile, filename: str) -> str:
    """Generate upload path for validator resource files."""
    # Use resource file's own UUID for uniqueness (shorter path than nested UUID)
    # Format: resource_files/<resource_uuid>/<filename>
    return f"resource_files/{instance.id}/{filename}"


class ValidatorResourceFile(TimeStampedModel):
    """
    Auxiliary files needed by advanced validators to run.

    Resource files are validator-specific files (weather data, libraries, configs)
    that are not submission data but are required for validation. Examples:
    - Weather files (EPW) for EnergyPlus simulations
    - Libraries for FMU validators
    - Configuration files for custom validators

    ## Scoping

    Resource files can be system-wide or organization-specific:
    - `org=NULL`: System-wide resource, visible to all organizations
    - `org=<org>`: Organization-specific, only visible to that org

    System admins can create system-wide resources. Org admins can create
    org-specific resources.

    ## Usage Flow

    1. Admin uploads resource file via Validator Library UI
    2. Workflow step editor shows dropdown of available resources (filtered by type)
    3. User selects resource → a ``WorkflowStepResource`` row is created linking
       the step to this file (FK-backed, PROTECT on delete)
    4. At execution time, envelope builder resolves step resources to storage URIs
    5. Validator container downloads files from URIs
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    validator = models.ForeignKey(
        Validator,
        on_delete=models.CASCADE,
        related_name="resource_files",
        help_text=_("The validator this resource file is for."),
    )

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="validator_resource_files",
        null=True,
        blank=True,
        help_text=_(
            "Owning organization (NULL for system-wide resources visible to all orgs)."
        ),
    )

    resource_type = models.CharField(
        max_length=32,
        choices=ResourceFileType.choices,
        help_text=_("Type of resource (weather, library, config, etc.)."),
    )

    name = models.CharField(
        max_length=200,
        help_text=_("Human-readable name (e.g., 'San Francisco TMY3')."),
    )

    filename = models.CharField(
        max_length=255,
        help_text=_("Original filename (e.g., 'USA_CA_San.Francisco...epw')."),
    )

    file = models.FileField(
        upload_to=_resource_file_upload_path,
        max_length=500,
        help_text=_(
            "The resource file stored in default media storage (public/ prefix)."
        ),
    )

    is_default = models.BooleanField(
        default=False,
        help_text=_(
            "Mark this resource as a default when displaying to workflow authors. "
            "System defaults are shown to all orgs; org defaults only to that org."
        ),
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_resource_files",
        help_text=_("User who uploaded this resource file."),
    )

    description = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional description or notes about this resource."),
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Validator-specific metadata (e.g., location info for weather)."),
    )

    # ADR-2026-04-27 Phase 3, task 11: SHA-256 of the file's bytes.
    # Computed on save, used to detect drift on resources referenced
    # by locked workflows. Empty until the first save; subsequent
    # saves recompute and (for catalog files used by locked
    # workflows) refuse to overwrite a different content hash.
    content_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "SHA-256 of the file's content, computed on save. Used to "
            "detect drift when a catalog file referenced by a locked "
            "workflow tries to mutate."
        ),
    )

    class Meta:
        ordering = ["-is_default", "name"]
        indexes = [
            models.Index(fields=["validator", "resource_type"]),
            models.Index(fields=["org", "resource_type"]),
        ]

    def is_used_by_locked_workflow(self) -> bool:
        """Return True if any versioned locked/used workflow references this file.

        Catalog files are referenced from a workflow step via
        ``WorkflowStepResource.validator_resource_file``. The check
        walks that one FK and gates on the same workflow signal as Ruleset
        (versioned history AND locked OR has-runs).
        """
        # Local import to avoid the circular workflows<->validations
        # module load.
        from validibot.workflows.constants import WorkflowHistoryPolicy
        from validibot.workflows.models import WorkflowStepResource

        return (
            WorkflowStepResource.objects.filter(
                validator_resource_file=self,
                step__workflow__history_policy=WorkflowHistoryPolicy.VERSIONED,
            )
            .filter(
                Q(step__workflow__is_locked=True)
                | Q(step__workflow__validation_runs__isnull=False),
            )
            .exists()
        )

    def _check_content_drift_or_raise(self, new_hash: str) -> None:
        """Raise if our stored hash differs from ``new_hash`` AND we're locked.

        Called from save() when there's a chance we're persisting a
        different file. The first-ever save populates the hash from
        empty; that's not drift.
        """
        if not self.pk or not self.content_hash:
            return  # uninitialised → nothing to drift from
        if new_hash == self.content_hash:
            return  # bytes unchanged
        # Hash changed — only block if a locked workflow depends on us.
        if self.is_used_by_locked_workflow():
            from django.core.exceptions import ValidationError

            raise ValidationError(
                {
                    "file": _(
                        "This catalog resource file is referenced by a "
                        "workflow that has runs (or is locked); its "
                        "bytes cannot change in place. Upload a new "
                        "ValidatorResourceFile entry instead."
                    ),
                },
            )

    def save(self, *args, **kwargs):
        """Compute and persist ``content_hash`` on every save.

        Performs the drift check before the row is written, so a
        rejected mutation never reaches the DB. Hash is read from
        storage, so the value reflects on-disk bytes after upload
        handlers have settled.
        """
        from validibot.core.filesafety import sha256_field_file

        new_hash = sha256_field_file(self.file)
        self._check_content_drift_or_raise(new_hash)
        self.content_hash = new_hash
        super().save(*args, **kwargs)

    def clean(self):
        from validibot.validations.constants import get_resource_type_config

        super().clean()
        config = get_resource_type_config(self.resource_type)
        if config and self.filename:
            ext = (
                self.filename.rsplit(".", 1)[-1].lower() if "." in self.filename else ""
            )
            if ext not in config.allowed_extensions:
                allowed = ", ".join(sorted(config.allowed_extensions))
                raise ValidationError(
                    {
                        "filename": _(
                            "File extension '.%(ext)s' is not allowed for "
                            "%(type)s. Allowed: %(allowed)s."
                        )
                        % {"ext": ext, "type": config.description, "allowed": allowed},
                    },
                )

    def __str__(self):
        scope = "system" if self.org is None else f"org:{self.org_id}"
        return f"{self.name} ({self.resource_type}, {scope})"

    @property
    def is_system(self) -> bool:
        """True if this is a system-wide resource (no org)."""
        return self.org_id is None

    def get_storage_uri(self) -> str:
        """
        Get the storage URI for this resource file.

        Returns a URI that can be used by validator containers to download
        the file. The URI scheme depends on the storage backend:
        - Local storage: file:///path/to/file
        - GCS: gs://bucket/location/path/to/file

        Note: Resource files are stored via Django's FileField (media storage),
        so we use the file's actual storage path to construct the URI.

        Important: For GCS, the storage may have a `location` prefix (e.g., "public")
        that's not included in file.name. We must include it in the URI.
        """
        # Get the actual file path from Django's storage
        file_storage = self.file.storage

        # Check if this is GCS storage (django-storages GoogleCloudStorage)
        storage_class_name = file_storage.__class__.__name__
        if storage_class_name == "GoogleCloudStorage":
            # GCS: gs://bucket/location/path
            # The storage may have a location prefix (e.g., "public") that
            # isn't included in file.name but IS part of the actual object path
            bucket_name = getattr(file_storage, "bucket_name", "")
            location = getattr(file_storage, "location", "")
            if location:
                return f"gs://{bucket_name}/{location}/{self.file.name}"
            return f"gs://{bucket_name}/{self.file.name}"

        # Local filesystem storage
        # The file.path gives the absolute path
        file_path = self.file.path
        return f"file://{file_path}"

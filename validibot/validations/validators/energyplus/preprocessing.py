"""EnergyPlus submission preprocessing — template resolution.

When a workflow step uses a parameterized IDF template, the submitter
uploads a JSON dict of variable values instead of a complete IDF file.
This module resolves that JSON into a full IDF **before** the submission
reaches any execution backend (Cloud Run, Docker Compose, etc.).

After preprocessing, the submission looks identical to a direct-IDF
upload — backends never need to know templates exist.  This is the key
design principle from the Parameterized Templates ADR (Section 7):
*"All template intelligence lives in Django.  The container stays
simple — it receives a fully resolved IDF."*

**Signal-binding resolution (Phase 4b):** When the step has
``StepSignalBinding`` rows for template signals, the resolution engine
resolves values via ``resolve_input_signal()`` which supports nested
JSON payloads and ``source_data_path`` expressions. The legacy flat-JSON
path is used as a fallback for steps without signal bindings.

The preprocessing is invoked by ``EnergyPlusValidator.preprocess_submission()``
which is called by ``AdvancedValidator.validate()`` before building the
``ExecutionRequest``.

See Also:
    - ``validibot.validations.utils.idf_template`` — merge/validate/substitute
    - ``validibot.validations.services.path_resolution`` — signal resolution engine
    - ``validibot.workflows.step_configs.get_step_config`` — typed config access
    - ``validibot.validations.validators.base.advanced.AdvancedValidator`` — hook
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from django.core.exceptions import ValidationError

from validibot.validations.utils.idf_template import IDF_UNSAFE_CHARS_PATTERN
from validibot.validations.utils.idf_template import MergeResult
from validibot.validations.utils.idf_template import decode_idf_bytes
from validibot.validations.utils.idf_template import substitute_template_parameters
from validibot.workflows.step_configs import get_step_config

if TYPE_CHECKING:
    from validibot.submissions.models import Submission
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


# ── Data structures ─────────────────────────────────────────────────


@dataclass
class PreprocessingResult:
    """Result of preprocessing an EnergyPlus submission.

    Attributes:
        was_template: True if the submission was a parameterized template
            (JSON parameters resolved into an IDF).  False for direct-IDF
            submissions where no preprocessing was needed.
        template_metadata: Metadata about the template resolution, stored
            in ``step_run.output`` for the results display.  Empty dict
            when ``was_template`` is False.
    """

    was_template: bool
    template_metadata: dict[str, object] = field(default_factory=dict)


# ── Public API ──────────────────────────────────────────────────────


def preprocess_energyplus_submission(
    *,
    step: WorkflowStep,
    submission: Submission,
) -> PreprocessingResult:
    """Resolve a parameterized template submission into a full IDF.

    If the step has a ``MODEL_TEMPLATE`` resource, the submission is
    treated as a JSON dict of variable values.  The function:

    1. Reads the template IDF from the step-owned resource file.
    2. Parses and validates the JSON submission as flat parameters.
    3. Merges submitter values with author defaults and validates
       constraints via ``merge_and_validate_template_parameters()``.
    4. Substitutes ``$VARIABLE`` placeholders via
       ``substitute_template_parameters()``.
    5. Overwrites ``submission.content`` with the resolved IDF so that
       all downstream consumers (backends, envelope builders) see a
       normal IDF file.

    If the step has no template resource, this is a direct-IDF
    submission and the function returns immediately (no-op).

    Args:
        step: WorkflowStep instance with ``step_resources`` relation.
        submission: Submission instance.  Modified in-place for template
            mode — ``content`` and ``original_filename`` are overwritten.

    Returns:
        PreprocessingResult with ``was_template`` flag and metadata.

    Raises:
        ValidationError: If JSON parsing, parameter validation,
            constraint checking, or template file I/O/decoding fails.
            The caller (``AdvancedValidator.validate()``) converts this
            into a user-friendly ``ValidationResult``.
    """
    # ── 1. Detect template mode ─────────────────────────────────
    template_resource = step.step_resources.filter(
        role="MODEL_TEMPLATE",
    ).first()

    if not template_resource:
        return PreprocessingResult(was_template=False)

    logger.info(
        "Template mode detected for step %s — resolving submission",
        step.id,
    )

    # ── 2. Read the template IDF ────────────────────────────────
    template_content = _read_template_content(template_resource)

    # ── 3. Parse submission and resolve parameters ────────────────
    #
    # Resolve template parameters via StepSignalBinding rows. Every
    # EnergyPlus template step should have signal bindings created by
    # sync_step_template_signals(). The bindings support nested JSON
    # payloads and source_data_path expressions.
    typed_config = get_step_config(step)

    merge_result = _resolve_via_signal_bindings(
        step=step,
        submission=submission,
        case_sensitive=typed_config.case_sensitive,
    )

    # ── 5. Substitute placeholders ──────────────────────────────
    resolved_idf = substitute_template_parameters(
        idf_text=template_content,
        parameters=merge_result.parameters,
        case_sensitive=typed_config.case_sensitive,
    )

    # ── 6. Overwrite the submission in memory ───────────────────
    # Submission.get_content() checks self.content before self.input_file,
    # so setting .content swaps in the resolved IDF for all downstream
    # consumers without touching the persisted file (which stays as the
    # original JSON for audit).
    submission.content = resolved_idf
    submission.original_filename = "resolved_model.idf"

    logger.info(
        "Template resolved: %d parameters, %d warnings",
        len(merge_result.parameters),
        len(merge_result.warnings),
    )

    # ── 7. Return metadata ──────────────────────────────────────
    template_metadata: dict[str, object] = {
        "template_parameters_used": merge_result.parameters,
        "template_warnings": merge_result.warnings,
    }

    return PreprocessingResult(
        was_template=True,
        template_metadata=template_metadata,
    )


# ── Internal helpers ────────────────────────────────────────────────


def _read_template_content(template_resource) -> str:
    """Read template IDF text from a step-owned resource file.

    Falls back to Latin-1 if UTF-8 decoding fails — this matches the
    upload validator's acceptance of Latin-1 encoded IDF files.

    Raises:
        ValidationError: If the file cannot be read from storage or
            cannot be decoded.  This propagates cleanly through
            ``AdvancedValidator.validate()``'s existing ``except
            ValidationError`` handler.
    """
    # I/O errors (deleted file, storage backend down) should surface as
    # ValidationError so the caller's existing handler catches them
    # instead of producing an unhandled 500.
    try:
        raw_bytes = template_resource.step_resource_file.read()
        template_resource.step_resource_file.seek(0)
    except OSError as exc:
        raise ValidationError(
            "Could not read template file from storage. "
            "The file may have been deleted. Please re-upload the template."
        ) from exc

    text, _ = decode_idf_bytes(raw_bytes)

    if text is None:
        raise ValidationError(
            "Template file could not be decoded as UTF-8 or Latin-1. "
            "Please re-upload the template with valid encoding."
        )

    return text


def _parse_submission_params(submission) -> dict[str, Any]:
    """Parse submission content as a flat JSON parameter dict.

    Validates that the content is a JSON object (not array/scalar)
    and that all values are flat (no nested objects or arrays).

    Raises:
        ValidationError: If the content is not valid flat JSON.
    """
    raw_content = submission.get_content()
    try:
        parsed = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationError(f"Submission content is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValidationError(
            "Template parameters must be a flat JSON object (e.g., "
            '{"U_FACTOR": "2.0", "SHGC": "0.25"}). '
            f"Received {type(parsed).__name__} instead."
        )

    # Reject nested objects/arrays — parameters must be flat key-value pairs.
    # This check only applies to the legacy path; the signal-binding path
    # allows nesting because source_data_path can navigate nested structures.
    nested_keys = [k for k, v in parsed.items() if isinstance(v, (dict, list))]
    if nested_keys:
        raise ValidationError(
            f"Template parameters must be flat key-value pairs. "
            f"Nested objects/arrays found for: {', '.join(nested_keys)}."
        )

    return parsed


def _parse_submission_data(submission) -> dict[str, Any]:
    """Parse submission content as a JSON dict, allowing nested structures.

    Unlike ``_parse_submission_params()`` (legacy, flat-only), this parser
    accepts nested objects and arrays — the signal resolution engine
    navigates them via ``source_data_path`` expressions.

    Raises:
        ValidationError: If the content is not valid JSON or not a dict.
    """
    raw_content = submission.get_content()
    try:
        parsed = json.loads(raw_content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationError(
            f"Submission content is not valid JSON: {exc}",
        ) from exc

    if not isinstance(parsed, dict):
        raise ValidationError(
            "Template parameters must be a JSON object. "
            f"Received {type(parsed).__name__} instead."
        )

    return parsed


def _step_has_template_bindings(step) -> bool:
    """Check whether the step has StepSignalBinding rows for template signals."""
    from validibot.validations.constants import SignalOriginKind

    return step.signal_bindings.filter(
        signal_definition__origin_kind=SignalOriginKind.TEMPLATE,
    ).exists()


def _resolve_via_signal_bindings(
    *,
    step,
    submission,
    case_sensitive: bool = True,
) -> MergeResult:
    """Resolve template parameters via StepSignalBinding + validate.

    This is the Phase 4b replacement for the legacy
    ``merge_and_validate_template_parameters()`` path. It:

    1. Parses the submission as a JSON dict (nesting allowed).
    2. Queries ``StepSignalBinding`` rows for template signals.
    3. Resolves each binding via ``resolve_input_signal()``.
    4. Validates resolved values against constraints stored in
       ``SignalDefinition.metadata`` (TemplateSignalMetadata).
    5. Returns a ``MergeResult`` with the merged parameter dict.

    Raises:
        ValidationError: If required parameters are missing or values
            fail type/range/safety validation.
    """
    from validibot.validations.constants import SignalDirection
    from validibot.validations.constants import SignalOriginKind
    from validibot.validations.models import StepSignalBinding
    from validibot.validations.services.path_resolution import resolve_input_signal

    submission_data = _parse_submission_data(submission)

    bindings = list(
        StepSignalBinding.objects.filter(
            workflow_step=step,
            signal_definition__direction=SignalDirection.INPUT,
            signal_definition__origin_kind=SignalOriginKind.TEMPLATE,
        )
        .select_related("signal_definition")
        .order_by("signal_definition__order")
    )

    merged: dict[str, str] = {}
    errors: list[str] = []
    warnings: list[str] = []

    # Detect whether any binding uses a nested source_data_path
    # (contains a dot or bracket). When true, the submission has a
    # structured layout — top-level keys are containers, not parameter
    # names — so unrecognized-parameter warnings and case normalization
    # of the submission data don't apply.
    uses_nested_paths = any(
        "." in (b.source_data_path or "") or "[" in (b.source_data_path or "")
        for b in bindings
    )

    # Case normalization only applies to flat submissions where
    # top-level keys ARE the variable names. For nested payloads,
    # the binding's source_data_path specifies the exact path, and
    # uppercasing top-level keys would break path resolution.
    if not case_sensitive and not uses_nested_paths:
        submission_data = {k.upper(): v for k, v in submission_data.items()}

    # Unrecognized-parameter check: only meaningful for flat payloads
    # where top-level keys should correspond to template variable names.
    # For nested payloads, top-level keys like "glazing" are structural
    # containers, not parameters — warning about them is noise.
    if not uses_nested_paths:
        expected_names = {b.signal_definition.native_name for b in bindings}
        flat_keys = set(submission_data.keys())
        extra = flat_keys - expected_names
        if extra:
            warnings.append(
                f"Unrecognized parameters (not in template): "
                f"{', '.join(sorted(extra))}. "
                f"Expected: {', '.join(sorted(expected_names))}. "
                f"Check for typos."
            )

    for binding in bindings:
        sig = binding.signal_definition
        name = sig.native_name

        resolved = resolve_input_signal(
            binding,
            submission_data=submission_data,
        )

        if resolved.resolved:
            value = str(resolved.value)
        elif binding.is_required:
            errors.append(
                f"Required parameter '{name}' is missing and has no "
                f"default. Description: "
                f"{sig.label or '(no description)'}."
            )
            continue
        else:
            # Optional signal, not found, no default — skip
            continue

        # Validate the resolved value against template constraints
        # stored in SignalDefinition.metadata (TemplateSignalMetadata).
        meta = sig.metadata or {}
        variable_type = meta.get("variable_type", "text")

        if variable_type == "number":
            _validate_number_from_metadata(name, value, meta, errors)
        elif variable_type == "choice":
            choices = meta.get("choices", [])
            if not choices:
                errors.append(
                    f"Parameter '{name}' has type 'choice' but no "
                    f"allowed values are defined."
                )
            elif value not in choices:
                errors.append(
                    f"Parameter '{name}' value '{value}' is not a valid "
                    f"choice. Allowed: {', '.join(choices)}."
                )
        elif variable_type == "text":
            if not value.strip():
                errors.append(f"Parameter '{name}' cannot be empty.")
            elif IDF_UNSAFE_CHARS_PATTERN.search(value):
                errors.append(
                    f"Parameter '{name}' contains characters that would "
                    f"corrupt the IDF file (comma, semicolon, !, or "
                    f"newline). Value: '{value[:50]}'."
                )

        merged[name] = value

    if errors:
        all_messages = errors + [f"Note: {w}" for w in warnings]
        raise ValidationError(all_messages)

    if warnings:
        logger.warning("Template parameter warnings: %s", "; ".join(warnings))

    return MergeResult(parameters=merged, warnings=warnings)


def _validate_number_from_metadata(
    name: str,
    value: str,
    meta: dict[str, Any],
    errors: list[str],
) -> None:
    """Validate a number-type value against TemplateSignalMetadata constraints.

    Mirrors the validation logic in ``idf_template._validate_number()`` but
    reads constraints from the SignalDefinition metadata dict instead of
    a TemplateVariable Pydantic object.
    """
    if value.strip().lower() in ("autosize", "autocalculate"):
        return

    try:
        num = float(value)
    except (ValueError, TypeError):
        errors.append(
            f"Parameter '{name}' must be a number (or "
            f"'Autosize'/'Autocalculate'), got '{value}'."
        )
        return

    min_value = meta.get("min_value")
    if min_value is not None:
        min_exclusive = meta.get("min_exclusive", False)
        if min_exclusive and num <= min_value:
            errors.append(
                f"Parameter '{name}' value {num} must be greater than {min_value}."
            )
        elif not min_exclusive and num < min_value:
            errors.append(
                f"Parameter '{name}' value {num} is below minimum {min_value}."
            )

    max_value = meta.get("max_value")
    if max_value is not None:
        max_exclusive = meta.get("max_exclusive", False)
        if max_exclusive and num >= max_value:
            errors.append(
                f"Parameter '{name}' value {num} must be less than {max_value}."
            )
        elif not max_exclusive and num > max_value:
            errors.append(
                f"Parameter '{name}' value {num} is above maximum {max_value}."
            )

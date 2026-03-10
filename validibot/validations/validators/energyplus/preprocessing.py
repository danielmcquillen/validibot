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

The preprocessing is invoked by ``EnergyPlusValidator.preprocess_submission()``
which is called by ``AdvancedValidator.validate()`` before building the
``ExecutionRequest``.

See Also:
    - ``validibot.validations.utils.idf_template`` — merge/validate/substitute
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

from validibot.validations.utils.idf_template import decode_idf_bytes
from validibot.validations.utils.idf_template import (
    merge_and_validate_template_parameters,
)
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
        ValidationError: If JSON parsing, parameter validation, or
            constraint checking fails.  The caller
            (``AdvancedValidator.validate()``) converts this into a
            user-friendly ``ValidationResult``.
        ValueError: If template substitution fails (unresolved variables).
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

    # ── 3. Parse and validate the JSON submission ───────────────
    submitter_params = _parse_submission_params(submission)

    # ── 4. Merge and validate parameters ────────────────────────
    typed_config = get_step_config(step)

    merge_result = merge_and_validate_template_parameters(
        submitter_params=submitter_params,
        template_variables=typed_config.template_variables,
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
    """
    raw_bytes = template_resource.step_resource_file.read()
    template_resource.step_resource_file.seek(0)

    text, _ = decode_idf_bytes(raw_bytes)

    if text is None:
        msg = (
            "Template file could not be decoded as UTF-8 or Latin-1. "
            "Please re-upload the template with valid encoding."
        )
        raise ValueError(msg)

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
    nested_keys = [k for k, v in parsed.items() if isinstance(v, (dict, list))]
    if nested_keys:
        raise ValidationError(
            f"Template parameters must be flat key-value pairs. "
            f"Nested objects/arrays found for: {', '.join(nested_keys)}."
        )

    return parsed

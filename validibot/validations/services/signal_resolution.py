"""Resolve workflow-level signal mappings against submission data.

This module provides the pre-step resolution phase: before any workflow
step executes, all ``WorkflowSignalMapping`` rows are resolved against
the submission payload.  The resulting dict is stored in
``RunContext.workflow_signals`` and injected into the CEL context as
the ``s`` / ``signal`` namespace.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.services.path_resolution import resolve_path

if TYPE_CHECKING:
    from validibot.workflows.models import Workflow

logger = logging.getLogger(__name__)


class SignalResolutionError(Exception):
    """Raised when a required signal cannot be resolved."""

    def __init__(self, signal_name: str, source_path: str, message: str):
        self.signal_name = signal_name
        self.source_path = source_path
        super().__init__(message)


@dataclass
class SignalResolutionResult:
    """Result of resolving all workflow signal mappings.

    Attributes:
        signals: Dict mapping signal names to resolved values.
            Suitable for injection into the CEL context as ``s``.
        errors: List of resolution error messages (for reporting
            to the user when on_missing=error signals fail).
    """

    signals: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def resolve_workflow_signals(
    workflow: Workflow,
    submission_data: Any,
) -> SignalResolutionResult:
    """Resolve all signal mappings for a workflow against submission data.

    Iterates over ``WorkflowSignalMapping`` rows ordered by position,
    resolves each source path against the submission data using
    ``resolve_path()``, applies default values, and handles
    ``on_missing`` behavior.

    Args:
        workflow: The workflow whose signal mappings to resolve.
        submission_data: The parsed submission payload (dict or list).

    Returns:
        A ``SignalResolutionResult`` with the resolved signals dict and
        any error messages.

    Raises:
        SignalResolutionError: If any signal with ``on_missing=error``
            cannot be resolved and has no default value.
    """
    from validibot.workflows.models import WorkflowSignalMapping

    mappings = WorkflowSignalMapping.objects.filter(
        workflow=workflow,
    ).order_by("position")

    result = SignalResolutionResult()

    for mapping in mappings:
        value, found = resolve_path(submission_data, mapping.source_path)

        if found:
            result.signals[mapping.name] = value
            continue

        # Path not found — check for default value
        if mapping.default_value is not None:
            result.signals[mapping.name] = mapping.default_value
            continue

        # No default — handle on_missing
        if mapping.on_missing == "null":
            result.signals[mapping.name] = None
            logger.info(
                "Signal '%s' resolved to null (source path '%s' not found, "
                "on_missing=null).",
                mapping.name,
                mapping.source_path,
            )
            continue

        # on_missing == "error" — record error
        error_msg = (
            f"Required signal '{mapping.name}' could not be resolved. "
            f"Source path '{mapping.source_path}' was not found in the "
            f"submission data."
        )
        result.errors.append(error_msg)
        logger.warning(
            "Signal '%s' resolution failed: source path '%s' not found "
            "in submission data (on_missing=error).",
            mapping.name,
            mapping.source_path,
        )

    if result.errors:
        combined = "; ".join(result.errors)
        raise SignalResolutionError(
            signal_name="",
            source_path="",
            message=f"Signal resolution failed: {combined}",
        )

    return result


# ── Signal name validation ──────────────────────────────────────────────

# Reserved top-level CEL context keys. Signal names must not use these.
RESERVED_CEL_NAMES = frozenset(
    {
        "p",
        "payload",
        "s",
        "signal",
        "o",
        "output",
        "steps",
        "has",
        "mean",
        "percentile",
        "sum",
        "max",
        "min",
        "abs",
        "round",
        "duration",
        "is_int",
        "true",
        "false",
        "null",
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
        "ceil",
        "floor",
        "timestamp",
        "matches",
        "in",
    }
)

_CEL_IDENT_RE = re.compile(r"^[_a-zA-Z][_a-zA-Z0-9]*$")


def validate_signal_name(name: str) -> list[str]:
    """Validate a signal name and return a list of error messages.

    Checks that the name is a valid CEL identifier and is not a
    reserved name. Returns an empty list if valid.
    """
    errors: list[str] = []
    if not name:
        errors.append("Signal name is required.")
        return errors
    if not _CEL_IDENT_RE.match(name):
        errors.append(
            f"'{name}' is not a valid signal name. "
            "Use only letters, digits, and underscores; "
            "must start with a letter or underscore."
        )
    if name in RESERVED_CEL_NAMES:
        errors.append(f"'{name}' is a reserved name and cannot be used as a signal.")
    return errors


def validate_signal_name_unique(
    workflow_id: int,
    name: str,
    *,
    exclude_mapping_id: int | None = None,
    exclude_signal_def_id: int | None = None,
) -> list[str]:
    """Check that a signal name is unique within a workflow.

    Queries both ``WorkflowSignalMapping`` (mapped signals) and
    ``SignalDefinition`` (promoted outputs with non-empty signal_name)
    to enforce cross-table uniqueness at the application level.

    Returns an empty list if the name is unique, or a list of error
    messages if it conflicts.
    """
    from validibot.validations.models import SignalDefinition
    from validibot.workflows.models import WorkflowSignalMapping

    errors: list[str] = []

    # Check against other workflow signal mappings
    mapping_qs = WorkflowSignalMapping.objects.filter(
        workflow_id=workflow_id,
        name=name,
    )
    if exclude_mapping_id:
        mapping_qs = mapping_qs.exclude(pk=exclude_mapping_id)
    if mapping_qs.exists():
        errors.append(
            f"Signal '{name}' is already defined in the workflow's signal mapping."
        )

    # Check against promoted validator outputs
    promoted_qs = SignalDefinition.objects.filter(
        workflow_step__workflow_id=workflow_id,
        signal_name=name,
    ).exclude(signal_name="")
    if exclude_signal_def_id:
        promoted_qs = promoted_qs.exclude(pk=exclude_signal_def_id)
    if promoted_qs.exists():
        errors.append(
            f"Signal '{name}' is already used as a promoted output name "
            f"on a validator step in this workflow."
        )

    return errors

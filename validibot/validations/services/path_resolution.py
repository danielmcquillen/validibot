"""
Shared path resolution for traversing nested JSON data structures.

This module provides the single, canonical path resolution function used
throughout Validibot. It replaces previously separate implementations:

1. ``BaseValidator._resolve_path()`` — used for CEL context building
2. ``BasicAssertionEvaluator._resolve_path()`` — used for BASIC assertions

Both now delegate to ``resolve_path()`` defined here.

The path syntax supports dotted keys for dict traversal and bracket
notation for array indexing:

- ``"building.floor_area"`` → ``data["building"]["floor_area"]``
- ``"items[0].name"`` → ``data["items"][0]["name"]``
- ``"building.floors[0].zones[1].sensors[2]"`` → nested traversal
- ``"[0].id"`` → ``data[0]["id"]`` (root is a list)

Negative indices are rejected (return not-found). Wildcards, filters,
and slice notation are not supported.

See Also:
    - ADR-2026-03-18: Unified Signal Model and Data Path Resolution
    - ``validibot/validations/tests/test_resolve_path.py`` — 60+ tests
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from validibot.validations.models import ResolvedInputTrace
    from validibot.validations.models import SignalDefinition
    from validibot.validations.models import StepSignalBinding
    from validibot.validations.models import ValidationStepRun
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


def resolve_path(data: Any, path: str | None) -> tuple[Any, bool]:
    """Resolve a dotted/bracket path against a nested data structure.

    Traverses dicts by key and lists/tuples by integer index, following
    the path expression from left to right. Returns the resolved value
    and a boolean indicating whether the path was found.

    Args:
        data: The root data structure to traverse. Can be a dict, list,
            or any value (non-traversable types return not-found for
            any non-empty path).
        path: Dot-separated path with optional bracket indices.
            Examples: ``"building.floor_area"``, ``"items[0].name"``,
            ``"floors[0].zones[1].id"``, ``"[0].id"`` (root is list).
            When ``None`` or empty, returns ``(data, True)``.

    Returns:
        A tuple of ``(resolved_value, was_found)``:
        - ``(value, True)`` when the path resolves successfully.
        - ``(None, False)`` when any segment is missing, out of bounds,
          or the wrong type.

    Examples:
        >>> resolve_path({"a": {"b": 42}}, "a.b")
        (42, True)
        >>> resolve_path({"items": [{"id": 1}]}, "items[0].id")
        (1, True)
        >>> resolve_path({"a": 1}, "a.b.c")
        (None, False)
        >>> resolve_path({"a": 1}, None)
        ({"a": 1}, True)
    """
    if not path:
        return data, True

    # Delegate filter expressions to the restricted JSONPath environment.
    if "[?" in str(path):
        from validibot.validations.services._jsonpath_env import resolve_jsonpath

        return resolve_jsonpath(data, str(path))

    current = data
    tokens = str(path).split(".")

    for token in tokens:
        if not token:
            continue

        # Check for bracket notation: "key[0]", bare "[0]",
        # or chained brackets "key[0][1]" / "[0][1]"
        if "[" in token and token.endswith("]"):
            # Split into leading key (may be empty) and bracket segments.
            # "matrix[0][1]" → key="matrix", brackets="[0][1]"
            # "[0][1]" → key="", brackets="[0][1]"
            first_bracket = token.index("[")
            key = token[:first_bracket]
            brackets = token[first_bracket:]

            # Navigate dict key first (if present)
            if key:
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    return None, False

            # Apply each bracket index in sequence.
            # "[0][1]" → ["0", "1"] after stripping brackets.
            for segment in brackets.split("["):
                if not segment:
                    continue
                index_str = segment.rstrip("]")
                try:
                    position = int(index_str)
                except ValueError:
                    return None, False

                if isinstance(current, (list, tuple)) and 0 <= position < len(current):
                    current = current[position]
                else:
                    return None, False

        # Plain dict key traversal
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            return None, False

    return current, True


# ── Signal resolution engine ─────────────────────────────────────────
#
# Higher-level functions that resolve input signals for a workflow step
# by reading StepSignalBinding rows and applying resolve_path() against
# the appropriate data scope (submission payload, metadata, or upstream
# step output).


class InputSignalResolutionError(Exception):
    """Raised when one or more required input signals cannot be resolved.

    Carries the ``signal_contract_key`` so callers can report which
    specific signal failed to resolve without parsing the message.

    The ``traces`` attribute holds the complete list of unsaved
    ``ResolvedInputTrace`` instances (both successes and failures)
    so the caller can persist them for diagnostics even when
    resolution fails.
    """

    def __init__(
        self,
        signal_contract_key: str,
        message: str,
        traces: list | None = None,
    ):
        self.signal_contract_key = signal_contract_key
        self.traces = traces or []
        super().__init__(message)


@dataclass
class ResolvedSignal:
    """Result of resolving a single input signal binding.

    Captures both the resolved value and the resolution metadata needed
    to create a ``ResolvedInputTrace`` audit row.
    """

    signal_definition: SignalDefinition
    binding: StepSignalBinding
    value: Any = None
    resolved: bool = False
    used_default: bool = False
    source_scope_used: str = ""
    source_data_path_used: str = ""
    upstream_step_key: str = ""
    error_message: str = ""


def resolve_input_signal(
    binding: StepSignalBinding,
    *,
    submission_data: dict[str, Any] | None = None,
    submission_metadata: dict[str, Any] | None = None,
    upstream_signals: dict[str, dict[str, Any]] | None = None,
) -> ResolvedSignal:
    """Resolve a single input signal from its binding configuration.

    Looks up the value in the data scope specified by
    ``binding.source_scope``, using ``binding.source_data_path`` as the
    path expression. Falls back to ``binding.default_value`` when the
    path doesn't resolve.

    Args:
        binding: The ``StepSignalBinding`` to resolve.
        submission_data: The submission payload dict (for SUBMISSION_PAYLOAD scope).
        submission_metadata: Submission metadata dict (for SUBMISSION_METADATA scope).
        upstream_signals: Dict of ``{step_key: {"signals": {...}}}`` from prior
            steps (for UPSTREAM_STEP scope).

    Returns:
        A ``ResolvedSignal`` with the resolved value and audit metadata.
        When a required signal cannot be resolved, the returned
        ``ResolvedSignal`` has ``resolved=False`` and a populated
        ``error_message``. The caller (``resolve_step_input_signals``)
        collects all results — including errors — before raising
        ``InputSignalResolutionError`` so audit traces are preserved.
    """
    from validibot.validations.constants import BindingSourceScope

    sig = binding.signal_definition
    scope = binding.source_scope
    path = binding.source_data_path

    # ADR-2026-03-18: when source_data_path is empty, fall back to
    # matching by contract_key as a top-level key in the scoped data.
    effective_path = path if path else sig.contract_key

    result = ResolvedSignal(
        signal_definition=sig,
        binding=binding,
        source_scope_used=scope,
        source_data_path_used=effective_path,
    )

    # Select the data source for this scope.
    if scope == BindingSourceScope.SUBMISSION_PAYLOAD:
        source = submission_data or {}
    elif scope == BindingSourceScope.SUBMISSION_METADATA:
        source = submission_metadata or {}
    elif scope == BindingSourceScope.UPSTREAM_STEP:
        # Upstream signals are stored at:
        #   run.summary["steps"][step_key]["signals"][signal_name]
        # The effective_path should be "step_key.signal_name", which
        # resolve_path navigates as: upstream[step_key]["signal_name"].
        # We flatten the nesting by building a dict of
        #   {step_key: {signal_name: value, ...}} from the raw shape.
        raw = upstream_signals or {}
        source = {
            k: v.get("signals", {}) if isinstance(v, dict) else {}
            for k, v in raw.items()
        }
        # Extract the upstream step_key from the first path segment
        # (e.g., "simulation.site_eui" → step_key="simulation") for
        # the audit trace.
        if "." in effective_path:
            result.upstream_step_key = effective_path.split(".", 1)[0]
    else:
        # SYSTEM scope — reserved for future use
        source = {}

    value, found = resolve_path(source, effective_path)

    if found:
        result.value = value
        result.resolved = True
        return result

    # Path didn't resolve — try default value.
    if binding.default_value is not None:
        result.value = binding.default_value
        result.resolved = True
        result.used_default = True
        return result

    # No value, no default — mark as unresolved.
    result.resolved = False
    if binding.is_required:
        result.error_message = (
            f"Required signal '{sig.contract_key}' could not be resolved "
            f"from {scope} at path '{path}'"
        )
    return result


def resolve_step_input_signals(
    step: WorkflowStep,
    step_run: ValidationStepRun,
    *,
    submission_data: dict[str, Any] | None = None,
    submission_metadata: dict[str, Any] | None = None,
    upstream_signals: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[ResolvedInputTrace]]:
    """Batch-resolve all input signal bindings for a workflow step.

    Queries all ``StepSignalBinding`` rows for the step's input signals,
    resolves each one, and returns:

    1. A dict mapping ``native_name`` → resolved value, suitable for
       passing as FMU ``start_values`` or EnergyPlus template parameters.
    2. A list of unsaved ``ResolvedInputTrace`` model instances for
       bulk creation by the caller.

    **Collect-then-raise:** All bindings are resolved and all audit traces
    are built before any error is raised. This ensures that operators get
    complete diagnostic information (which signals resolved, which failed)
    even when one or more required signals are missing.

    Raises:
        InputSignalResolutionError: If any required signal fails to resolve.
            The error message lists ALL missing required signals, not just
            the first one encountered.
    """
    from validibot.validations.constants import SignalDirection
    from validibot.validations.models import ResolvedInputTrace
    from validibot.validations.models import StepSignalBinding

    bindings = (
        StepSignalBinding.objects.filter(
            workflow_step=step,
            signal_definition__direction=SignalDirection.INPUT,
        )
        .select_related("signal_definition")
        .order_by("signal_definition__order")
    )

    input_values: dict[str, Any] = {}
    traces: list[ResolvedInputTrace] = []
    errors: list[str] = []

    for binding in bindings:
        resolved = resolve_input_signal(
            binding,
            submission_data=submission_data,
            submission_metadata=submission_metadata,
            upstream_signals=upstream_signals,
        )

        if resolved.resolved:
            # Use the native_name as the key (FMU variable name or
            # template placeholder) since that's what the runner expects.
            native = resolved.signal_definition.native_name
            input_values[native] = resolved.value
        elif resolved.error_message:
            # Required signal failed — collect the error but don't
            # raise yet so we can build traces for all signals first.
            errors.append(resolved.error_message)

        traces.append(
            ResolvedInputTrace(
                step_run=step_run,
                signal_definition=resolved.signal_definition,
                signal_contract_key=resolved.signal_definition.contract_key,
                source_scope_used=resolved.source_scope_used,
                source_data_path_used=resolved.source_data_path_used,
                upstream_step_key=resolved.upstream_step_key,
                resolved=resolved.resolved,
                used_default=resolved.used_default,
                value_snapshot=resolved.value if resolved.resolved else None,
                error_message=resolved.error_message,
            ),
        )

    # Raise after all traces are built. The traces are attached to the
    # exception so the caller can persist them for diagnostics even
    # when resolution fails.
    if errors:
        raise InputSignalResolutionError(
            signal_contract_key=errors[0].split("'")[1] if "'" in errors[0] else "",
            message="; ".join(errors),
            traces=traces,
        )

    return input_values, traces

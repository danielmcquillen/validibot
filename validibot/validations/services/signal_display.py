"""
Output signal display helpers for validation run results.

Provides shared enrichment logic used by both the web UI
(``ValidationRunDetailView``) and the API (``ValidationRunSerializer``).
Given a ``ValidationStepRun`` whose ``output`` contains raw signal data,
these helpers:

1. Filter signals by the author's ``display_step_outputs`` selection.
   **Empty list means show NONE** — the author must opt in to each
   signal they want surfaced to the submitter. A future workflow-step
   toggle ("show all output signals") will give one-click access for
   cases where every signal is wanted.
2. Enrich each signal with human-readable metadata (label, units,
   precision) from the validator's ``StepIODefinition`` records.
3. Format numeric values with thousands separators and configurable
   decimal precision.

This is a **cross-validator capability** — any validator type that
populates ``step_run.output["signals"]`` gets signal display
automatically.  The ``display_step_outputs`` filter is read via
``getattr(typed_config, "display_step_outputs", [])`` so validators whose
config model lacks that field simply surface no signals (consistent
with the opt-in default).

See Also:
    - ``EnergyPlusStepConfig.display_step_outputs`` (``workflows/step_configs.py``)
    - ``StepIODefinition`` (``validations/models.py``)
    - ``AdvancedValidationProcessor.store_signals()`` (persists signals)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from validibot.validations.models import StepIODefinition
    from validibot.validations.models import ValidationStepRun

logger = logging.getLogger(__name__)

# Default number of decimal places for float formatting when the catalog
# entry does not specify a precision.
_DEFAULT_PRECISION = 2


# ── Data structures ─────────────────────────────────────────────────


@dataclass
class DisplaySignal:
    """A single output signal enriched with signal definition metadata for display.

    Attributes:
        slug: Machine name matching the signal's contract_key (e.g.,
            ``"site_electricity_kwh"``).
        label: Human-readable label (e.g., ``"Site Electricity"``).
        value: Raw signal value as stored in ``step_run.output``.
        formatted_value: Pre-formatted string for UI display (e.g.,
            ``"12,345.60"``).
        units: Display units (e.g., ``"kWh"``).  Empty string when
            the signal definition has no unit.
        description: Longer description from the signal definition.
        order: Sort key from the signal definition's ``order`` field.
    """

    slug: str
    label: str
    value: Any
    formatted_value: str
    units: str
    description: str
    order: int


# ── Public API ───────────────────────────────────────────────────────


def build_display_step_outputs(step_run: ValidationStepRun) -> list[DisplaySignal]:
    """Build display-ready signals for a single step run.

    Steps:
        1. Read raw signals from ``step_run.output["signals"]``.
        2. Determine which signals to show using the step config's
           ``display_step_outputs`` list (empty = show all).
        3. Batch-fetch ``StepIODefinition`` records for enrichment
           (labels, units, precision).
        4. Format each value and return an ordered list.

    Returns an empty list if the step has no signal data.
    """
    output = step_run.output or {}
    raw_signals: dict[str, Any] = output.get("signals", {})
    if not raw_signals:
        return []

    # Determine which signals to display.
    #
    # Default (empty filter) is now "show NONE" — authors opt in to
    # each signal they want exposed in the run response. This protects
    # against accidentally surfacing internal diagnostic signals to
    # the submitter, and it matches the principle of least surprise:
    # the API only returns what the workflow author explicitly chose.
    #
    # A future "Show all output signals" workflow-step toggle is
    # tracked in validibot-project. Until that ships, the only way to
    # show all signals is to enumerate them in ``display_step_outputs``.
    display_filter = _get_display_step_outputs_filter(step_run)
    if not display_filter:
        return []
    # Preserve the author's ordering by iterating display_filter.
    visible_slugs = [s for s in display_filter if s in raw_signals]

    if not visible_slugs:
        return []

    # Batch-fetch signal definition metadata.
    signal_map = _build_signal_map(step_run, visible_slugs)

    # Build enriched signals.
    result: list[DisplaySignal] = []
    for slug in visible_slugs:
        value = raw_signals[slug]
        signal = signal_map.get(slug)

        label = slug.replace("_", " ").title()
        units = ""
        precision = None
        description = ""
        order = 999

        if signal:
            label = signal.label or label
            units = signal.unit or ""
            precision = (signal.metadata or {}).get("precision")
            description = signal.description or ""
            order = signal.order

        formatted = _format_signal_value(value, precision)
        result.append(
            DisplaySignal(
                slug=slug,
                label=label,
                value=value,
                formatted_value=formatted,
                units=units,
                description=description,
                order=order,
            ),
        )

    result.sort(key=lambda s: s.order)
    return result


def build_template_params_display(
    step_run: ValidationStepRun,
) -> list[dict[str, str]]:
    """Build display-ready template parameter data for a step run.

    Merges ``step_run.output["template_parameters_used"]`` with variable
    metadata to produce a list of ``{"name", "label", "value", "units"}``
    dicts suitable for the "Parameters Used" section in results.

    Metadata is sourced from ``StepIODefinition`` rows for the step.

    Returns an empty list for non-template runs (no
    ``template_parameters_used`` key in output).
    """
    output = step_run.output or {}
    params: dict[str, str] | None = output.get("template_parameters_used")
    if not params:
        return []

    var_meta: dict[str, dict[str, str]] = {}
    workflow_step = getattr(step_run, "workflow_step", None)
    if workflow_step:
        var_meta = _build_template_param_meta(workflow_step)

    result: list[dict[str, str]] = []
    for name, value in params.items():
        meta = var_meta.get(name, {})
        result.append(
            {
                "name": name,
                "label": meta.get("label", name),
                "value": value,
                "units": meta.get("units", ""),
            },
        )
    return result


def _build_template_param_meta(
    workflow_step,
) -> dict[str, dict[str, str]]:
    """Build a metadata lookup for template parameters.

    Iterates ``StepIODefinition`` rows for the step and filters to
    template-origin ones in Python. ``.all()`` (rather than
    ``.filter(origin_kind=...)``) is deliberate — Django's prefetch
    cache only populates the ``.all()`` result, so callers that
    pre-fetched ``workflow_step__signal_definitions`` at the
    queryset level (see ``OrgScopedRunViewSet.get_queryset`` and
    ``ValidationRunViewSet.get_queryset``) get zero extra queries
    per step. Callers that didn't prefetch still work — they pay
    one query per step, same as before the refactor — so the
    change is strictly an optimisation, not a behavioural one.

    See refactor-step item ``[review-#5]`` (amendment for
    signal-bearing runs).
    """
    from validibot.validations.constants import SignalOriginKind

    meta: dict[str, dict[str, str]] = {}
    for sig in workflow_step.signal_definitions.all():
        if sig.origin_kind != SignalOriginKind.TEMPLATE:
            continue
        meta[sig.native_name] = {
            "label": sig.label or sig.native_name,
            "units": sig.unit or "",
        }
    return meta


# ── Internal helpers ─────────────────────────────────────────────────


def _get_display_step_outputs_filter(step_run: ValidationStepRun) -> list[str]:
    """Extract the ``display_step_outputs`` list from the step's display settings.

    ``display_step_outputs`` is cosmetic, so it lives in the ``display_settings``
    bucket (ADR-2026-06-18). Returns an empty list when:
    - The step has no workflow_step (defensive).
    - The author left ``display_step_outputs`` empty (backward-compatible
      default: show all signals).
    """
    workflow_step = getattr(step_run, "workflow_step", None)
    if not workflow_step:
        return []
    try:
        display_settings = workflow_step.display_settings_typed
        return getattr(display_settings, "display_step_outputs", [])
    except (AttributeError, TypeError, KeyError, ValueError):
        # Parsing/attribute failures are safe to degrade (show all
        # signals).  Infrastructure errors (DatabaseError, etc.) are
        # intentionally NOT caught — they should propagate.
        logger.warning(
            "Could not read display_step_outputs from step config for step_run %s",
            step_run.id,
            exc_info=True,
        )
        return []


def _build_signal_map(
    step_run: ValidationStepRun,
    slugs: list[str],
) -> dict[str, StepIODefinition]:
    """Batch-fetch ``StepIODefinition`` records for the given slugs.

    Checks both validator-owned signals (library validators) and
    step-owned signals (step-level FMU uploads) to cover all origin
    kinds.  Returns a dict mapping contract_key → signal definition.

    Iterates ``.all()`` and filters by ``slug`` in Python rather
    than using ``.filter(contract_key__in=slugs)``. Django's
    prefetch cache only populates for ``.all()`` — a filtered
    queryset forces a fresh DB hit even when the prefetch already
    loaded every signal_definition for the step. The viewsets
    pre-fetch ``workflow_step__signal_definitions`` and
    ``workflow_step__validator__signal_definitions`` so listing
    runs with populated ``output["signals"]`` stays O(1) in step
    count.

    See refactor-step item ``[review-#5]`` (amendment for
    signal-bearing runs).
    """
    workflow_step = getattr(step_run, "workflow_step", None)
    if not workflow_step:
        return {}

    slug_set = set(slugs)

    # Step-owned signals (e.g., step-level FMU uploads) take priority.
    result: dict[str, StepIODefinition] = {}
    for sig in workflow_step.signal_definitions.all():
        if sig.contract_key in slug_set:
            result[sig.contract_key] = sig

    # Fill in from validator-owned signals (library validators).
    validator = getattr(workflow_step, "validator", None)
    if validator:
        for sig in validator.signal_definitions.all():
            if sig.contract_key in slug_set:
                result.setdefault(sig.contract_key, sig)

    return result


def _format_signal_value(
    value: Any,
    precision: int | None = None,
) -> str:
    """Format a signal value for human display.

    Formatting rules:
    - ``None``  → ``"N/A"``
    - ``int``   → thousands separators, no decimals (e.g., ``"12,345"``)
    - ``float`` → thousands separators + *precision* decimal places
      (default 2, e.g., ``"12,345.60"``)
    - ``str``   → passed through unchanged
    - Other     → ``str(value)``
    """
    if value is None:
        return "N/A"

    if isinstance(value, bool):
        # bool is a subclass of int — handle it before the int check.
        return str(value)

    if isinstance(value, int):
        return f"{value:,}"

    if isinstance(value, float):
        p = precision if precision is not None else _DEFAULT_PRECISION
        return f"{value:,.{p}f}"

    return str(value)

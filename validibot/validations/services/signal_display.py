"""
Output signal display helpers for validation run results.

Provides shared enrichment logic used by both the web UI
(``ValidationRunDetailView``) and the API (``ValidationRunSerializer``).
Given a ``ValidationStepRun`` whose ``output`` contains raw signal data,
these helpers:

1. Filter signals by the author's ``display_signals`` selection (or show
   all when the list is empty — backward-compatible default).
2. Enrich each signal with human-readable metadata (label, units,
   precision) from the validator's ``ValidatorCatalogEntry`` records.
3. Format numeric values with thousands separators and configurable
   decimal precision.

This is a **cross-validator capability** — any validator type that
populates ``step_run.output["signals"]`` gets signal display
automatically.  The ``display_signals`` filter is read via
``getattr(typed_config, "display_signals", [])`` so validators whose
config model lacks that field simply show all signals.

See Also:
    - ``EnergyPlusStepConfig.display_signals`` (``workflows/step_configs.py``)
    - ``ValidatorCatalogEntry`` (``validations/models.py``)
    - ``AdvancedValidationProcessor.store_signals()`` (persists signals)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from validibot.validations.models import ValidationStepRun
    from validibot.validations.models import ValidatorCatalogEntry

logger = logging.getLogger(__name__)

# Default number of decimal places for float formatting when the catalog
# entry does not specify a precision.
_DEFAULT_PRECISION = 2


# ── Data structures ─────────────────────────────────────────────────


@dataclass
class DisplaySignal:
    """A single output signal enriched with catalog metadata for display.

    Attributes:
        slug: Machine name matching the catalog entry (e.g.,
            ``"site_electricity_kwh"``).
        label: Human-readable label (e.g., ``"Site Electricity"``).
        value: Raw signal value as stored in ``step_run.output``.
        formatted_value: Pre-formatted string for UI display (e.g.,
            ``"12,345.60"``).
        units: Display units (e.g., ``"kWh"``).  Empty string when
            the catalog entry has no units metadata.
        description: Longer description from the catalog entry.
        order: Sort key from the catalog entry's ``order`` field.
    """

    slug: str
    label: str
    value: Any
    formatted_value: str
    units: str
    description: str
    order: int


# ── Public API ───────────────────────────────────────────────────────


def build_display_signals(step_run: ValidationStepRun) -> list[DisplaySignal]:
    """Build display-ready signals for a single step run.

    Steps:
        1. Read raw signals from ``step_run.output["signals"]``.
        2. Determine which signals to show using the step config's
           ``display_signals`` list (empty = show all).
        3. Batch-fetch ``ValidatorCatalogEntry`` records for
           enrichment (labels, units, precision).
        4. Format each value and return an ordered list.

    Returns an empty list if the step has no signal data.
    """
    output = step_run.output or {}
    raw_signals: dict[str, Any] = output.get("signals", {})
    if not raw_signals:
        return []

    # Determine which signals to display.
    display_filter = _get_display_signals_filter(step_run)
    if display_filter:
        # Preserve the author's ordering by iterating display_filter.
        visible_slugs = [s for s in display_filter if s in raw_signals]
    else:
        visible_slugs = list(raw_signals.keys())

    if not visible_slugs:
        return []

    # Batch-fetch catalog metadata.
    catalog_map = _build_catalog_map(step_run, visible_slugs)

    # Build enriched signals.
    result: list[DisplaySignal] = []
    for slug in visible_slugs:
        value = raw_signals[slug]
        catalog = catalog_map.get(slug)

        label = slug.replace("_", " ").title()
        units = ""
        precision = None
        description = ""
        order = 999

        if catalog:
            label = catalog.label or label
            meta = catalog.metadata or {}
            units = meta.get("units", "")
            precision = meta.get("precision")
            description = catalog.description or ""
            order = catalog.order

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
    metadata from the step config's ``template_variables`` to produce a
    list of ``{"name", "label", "value", "units"}`` dicts suitable for
    the "Parameters Used" section in results.

    Returns an empty list for non-template runs (no
    ``template_parameters_used`` key in output).
    """
    output = step_run.output or {}
    params: dict[str, str] | None = output.get("template_parameters_used")
    if not params:
        return []

    # Build a lookup of variable metadata from the step config.
    var_meta: dict[str, dict[str, str]] = {}
    workflow_step = getattr(step_run, "workflow_step", None)
    if workflow_step:
        try:
            typed_config = workflow_step.typed_config
            for v in getattr(typed_config, "template_variables", []):
                key = v.name
                var_meta[key] = {
                    "label": v.description or v.name,
                    "units": v.units,
                }
        except Exception:
            logger.warning(
                "Could not parse step config for template param display "
                "on step_run %s — falling back to raw variable names",
                step_run.id,
                exc_info=True,
            )

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


# ── Internal helpers ─────────────────────────────────────────────────


def _get_display_signals_filter(step_run: ValidationStepRun) -> list[str]:
    """Extract the ``display_signals`` list from the step's typed config.

    Returns an empty list when:
    - The step has no workflow_step (defensive).
    - The config model has no ``display_signals`` attribute (cross-
      validator: show all signals).
    - The author left ``display_signals`` empty (backward-compatible
      default).
    """
    workflow_step = getattr(step_run, "workflow_step", None)
    if not workflow_step:
        return []
    try:
        typed_config = workflow_step.typed_config
        return getattr(typed_config, "display_signals", [])
    except Exception:
        logger.warning(
            "Could not read display_signals from step config for step_run %s",
            step_run.id,
            exc_info=True,
        )
        return []


def _build_catalog_map(
    step_run: ValidationStepRun,
    slugs: list[str],
) -> dict[str, ValidatorCatalogEntry]:
    """Batch-fetch ``ValidatorCatalogEntry`` records for the given slugs.

    Returns a dict mapping slug → catalog entry.  Uses a single queryset
    filtered by ``slug__in`` for efficiency.
    """
    workflow_step = getattr(step_run, "workflow_step", None)
    if not workflow_step:
        return {}
    validator = getattr(workflow_step, "validator", None)
    if not validator:
        return {}
    entries = validator.catalog_entries.filter(slug__in=slugs)
    return {entry.slug: entry for entry in entries}


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

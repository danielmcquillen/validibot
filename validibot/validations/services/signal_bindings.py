"""
Ensure StepSignalBinding rows exist for all validator-owned input
signals on a workflow step.

Called after step creation/update so that the signal resolution
engine and envelope builder have bindings to work with. Without
bindings, the envelope builder falls back to legacy mode.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.models import SignalDefinition
from validibot.validations.models import StepSignalBinding
from validibot.validations.services.catalog_entry_normalization import (
    build_step_binding_defaults_from_mapping,
)
from validibot.validations.validators.base.config import get_config

if TYPE_CHECKING:
    from validibot.validations.validators.base.config import CatalogEntrySpec
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


def ensure_step_signal_bindings(step: WorkflowStep) -> int:
    """Create default StepSignalBinding rows for validator-owned input signals.

    For each input SignalDefinition owned by the step's validator that
    doesn't already have a binding on this step, creates a binding with:

    - source_scope/source_data_path: derived from the owning validator's
      catalog entry config when available
    - is_required/default_value: copied from the validator config defaults
      when declared
    - fallback defaults: submission payload + signal native name

    Returns the number of bindings created.
    """
    if not step.validator_id:
        return 0

    # Find all CATALOG-origin input signals owned by this validator.
    # We only handle CATALOG signals here — FMU and TEMPLATE signals
    # are managed by their own dedicated sync functions.
    validator_input_signals = SignalDefinition.objects.filter(
        validator_id=step.validator_id,
        direction=SignalDirection.INPUT,
        origin_kind=SignalOriginKind.CATALOG,
    )

    if not validator_input_signals.exists():
        return 0

    # Find which signals already have a binding on this step so we
    # don't overwrite any author-customised bindings.
    existing_signal_ids = set(
        StepSignalBinding.objects.filter(
            workflow_step=step,
            signal_definition__in=validator_input_signals,
        ).values_list("signal_definition_id", flat=True)
    )

    created = 0
    catalog_entries = _build_validator_catalog_entry_map(step)
    for sig in validator_input_signals:
        if sig.pk in existing_signal_ids:
            continue

        fallback_path = ""
        entry = catalog_entries.get((sig.contract_key, sig.direction))
        if entry:
            defaults = build_step_binding_defaults_from_mapping(
                entry.binding_config,
                fallback_path=fallback_path,
                default_required=entry.is_required,
            )
        else:
            defaults = build_step_binding_defaults_from_mapping(
                sig.provider_binding,
                fallback_path=fallback_path,
                default_required=True,
            )

        StepSignalBinding.objects.create(
            workflow_step=step,
            signal_definition=sig,
            source_scope=defaults["source_scope"],
            source_data_path=defaults["source_data_path"],
            is_required=defaults["is_required"],
            default_value=defaults["default_value"],
        )
        created += 1

    if created:
        logger.info(
            "Created %d default signal binding(s) for step %s (validator %s)",
            created,
            step.pk,
            step.validator_id,
        )

    return created


def _build_validator_catalog_entry_map(
    step: WorkflowStep,
) -> dict[tuple[str, str], CatalogEntrySpec]:
    """Index the system validator's catalog entries by ``(slug, run_stage)``.

    Validator-owned signals should derive their default step binding
    values from the current validator config, not from
    ``SignalDefinition.provider_binding``. The config remains the source
    of truth for library-signal defaults such as submission metadata
    paths and required/optional semantics.
    """
    validator = step.validator
    if not validator or not validator.is_system:
        return {}

    cfg = get_config(validator.validation_type)
    if not cfg or cfg.slug != validator.slug:
        return {}

    return {
        (entry.slug, entry.run_stage): entry
        for entry in cfg.catalog_entries
        if entry.entry_type == "signal"
    }

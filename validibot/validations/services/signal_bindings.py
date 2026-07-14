"""
Ensure StepInputBinding rows exist for all validator-owned input
signals on a workflow step.

Called after step creation/update so that the signal resolution
engine and envelope builder have bindings to work with. Without
bindings, launch fails closed for validators that declare external
inputs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.constants import StepIOMedium
from validibot.validations.models import StepInputBinding
from validibot.validations.models import StepIODefinition
from validibot.validations.services.catalog_entry_normalization import (
    build_step_binding_defaults_from_mapping,
)
from validibot.validations.validators.base.config import get_config

if TYPE_CHECKING:
    from validibot.validations.validators.base.config import CatalogEntrySpec
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


def ensure_step_signal_bindings(step: WorkflowStep) -> int:
    """Create default StepInputBinding rows for validator-owned input signals.

    For each input StepIODefinition owned by the step's validator that
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
    validator_input_signals = StepIODefinition.objects.filter(
        validator_id=step.validator_id,
        direction=SignalDirection.INPUT,
        origin_kind=SignalOriginKind.CATALOG,
    )

    if not validator_input_signals.exists():
        return 0

    # Find which signals already have a binding on this step so we
    # don't overwrite any author-customised bindings.
    existing_signal_ids = set(
        StepInputBinding.objects.filter(
            workflow_step=step,
            signal_definition__in=validator_input_signals,
        ).values_list("signal_definition_id", flat=True)
    )

    created = 0
    catalog_entries = _build_validator_catalog_entry_map(step)
    for sig in validator_input_signals:
        if sig.pk in existing_signal_ids:
            continue

        entry = catalog_entries.get((sig.contract_key, sig.direction))

        # ── P1-1 fix: skip parser-sourced step inputs ──────────────
        # Per ADR-2026-05-22, parser-extracted step inputs (catalog
        # entries with binding_config={"source": "parser", ...}) are
        # populated by the validator's extract_input_signals() at
        # runtime — no author-supplied payload path is involved. Creating
        # a StepInputBinding row for these would either dispatch-fail
        # (binding can't resolve a path that doesn't exist) or surface
        # them in the UI as user-mappable required inputs (wrong — the
        # validator owns these values). Skip them entirely.
        if entry and (entry.binding_config or {}).get("source") == "parser":
            continue

        if sig.io_medium == StepIOMedium.ARTIFACT:
            defaults = _build_artifact_binding_defaults(sig)
        elif entry:
            fallback_path = ""
            defaults = build_step_binding_defaults_from_mapping(
                entry.binding_config,
                fallback_path=fallback_path,
                default_required=entry.is_required,
            )
        else:
            fallback_path = ""
            defaults = build_step_binding_defaults_from_mapping(
                sig.provider_binding,
                fallback_path=fallback_path,
                default_required=True,
            )

        StepInputBinding.objects.create(
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
    ``StepIODefinition.provider_binding``. The config remains the source
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


def _build_artifact_binding_defaults(sig: StepIODefinition) -> dict[str, object]:
    """Return default binding values for a declared artifact input port."""

    if sig.resource_type:
        source_scope = BindingSourceScope.WORKFLOW_RESOURCE
        source_data_path = sig.resource_type
    else:
        source_scope = BindingSourceScope.SUBMISSION_FILE
        source_data_path = sig.role or sig.contract_key

    return {
        "source_scope": source_scope,
        "source_data_path": source_data_path,
        "default_value": None,
        "is_required": sig.min_items > 0,
    }

"""
Sync step-level FMU variables to ``SignalDefinition`` and ``StepSignalBinding``.

When a user uploads an FMU to a workflow step, ``introspect_fmu()`` extracts
the model's input/output variables. This module creates the corresponding
``SignalDefinition`` and ``StepSignalBinding`` rows that downstream features
(CEL context, signal display, assertion targeting) use as the single source
of truth for FMU signals.

The core function :func:`sync_step_fmu_signals` is called after ``step.save()``
during FMU upload, and can also be called from data migrations to backfill
existing steps.

**Reconciliation on re-upload:** When a user uploads a different FMU to the
same step, variable names may change. The function upserts by
``(workflow_step, contract_key, direction)`` and deletes orphaned signals.
Cascade-delete on ``StepSignalBinding`` ensures bindings are cleaned up too.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from slugify import slugify

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.constants import SignalSourceKind
from validibot.validations.models import SignalDefinition
from validibot.validations.models import StepSignalBinding
from validibot.validations.signal_metadata.metadata import FMUProviderBinding
from validibot.validations.signal_metadata.metadata import FMUSignalMetadata

if TYPE_CHECKING:
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


def sync_step_fmu_signals(
    step: WorkflowStep,
    fmu_variables: list[dict[str, Any]],
) -> None:
    """Create or update ``SignalDefinition`` and ``StepSignalBinding`` rows
    for every input/output variable in a step-level FMU upload.

    This is the step-owned counterpart to the library-validator dual-write
    in ``fmu._persist_variables()``. Library validators own signals via
    the ``validator`` FK; step-level FMU uploads own signals via the
    ``workflow_step`` FK.

    **contract_key stability on rename (deferred to Phase 6):**
    Currently, contract_key is derived from the FMU variable name. If a
    user re-uploads an FMU where a variable was renamed (e.g.,
    ``T_outdoor`` → ``T_ambient``), the old signal is deleted and a new
    one is created — which breaks any assertions targeting the old key.
    The ADR calls for stable contract_key preservation via FMI
    ``value_reference`` matching. Until then, renamed variables are
    treated as new signals. See ADR-2026-03-18, Phase 6.

    Args:
        step: The workflow step that owns these signals. Must be saved
            (have a PK) before calling.
        fmu_variables: List of variable dicts (from FMU introspection),
            each with keys: name, causality, variability, value_reference,
            value_type, unit, description, label.
    """
    if not step.pk:
        raise ValueError("Step must be saved before syncing FMU signals.")

    # Track which (contract_key, direction) tuples we create/update so we
    # can delete orphans afterward.
    seen: set[tuple[str, str]] = set()

    # Track contract_keys assigned in this batch to detect collisions
    # when two different FMU variables slugify to the same key.
    # We do NOT check against existing DB keys because on re-upload,
    # existing keys should be updated in place (via update_or_create),
    # not treated as collisions.
    batch_keys: set[str] = set()

    for var in fmu_variables:
        causality = (var.get("causality") or "").lower()
        direction = _direction_for_causality(causality)
        if not direction:
            # Skip parameter, local, independent, etc.
            continue

        name = var.get("name", "")
        if not name:
            continue

        # Generate a slug-safe contract_key from the FMU variable name.
        # FMU names like "T_outdoor" slugify to "t_outdoor"; names with
        # dots like "Panel.Area_m2" slugify to "panelarea_m2".
        base_key = slugify(name, separator="_") or "signal"
        contract_key = base_key
        counter = 2
        while contract_key in batch_keys:
            contract_key = f"{base_key}_{counter}"
            counter += 1
        batch_keys.add(contract_key)
        seen.add((contract_key, direction))

        # Upsert SignalDefinition
        sig, _created = SignalDefinition.objects.update_or_create(
            workflow_step=step,
            contract_key=contract_key,
            direction=direction,
            defaults={
                "native_name": name,
                "label": var.get("label") or "",
                "description": var.get("description") or "",
                "data_type": _data_type_for_fmu(var.get("value_type", "")),
                "unit": var.get("unit") or "",
                "origin_kind": SignalOriginKind.FMU,
                "source_kind": (
                    SignalSourceKind.PAYLOAD_PATH
                    if direction == SignalDirection.INPUT
                    else SignalSourceKind.INTERNAL
                ),
                "is_path_editable": direction == SignalDirection.INPUT,
                "provider_binding": FMUProviderBinding(
                    causality=causality,
                ).model_dump(),
                "metadata": FMUSignalMetadata(
                    variability=var.get("variability", ""),
                    value_reference=var.get("value_reference", 0),
                    value_type=var.get("value_type", ""),
                ).model_dump(),
            },
        )

        # Upsert StepSignalBinding for input signals.
        # Leave source_data_path empty — the user must map each input
        # to the correct payload path or signal for their data format.
        if direction == SignalDirection.INPUT:
            StepSignalBinding.objects.update_or_create(
                workflow_step=step,
                signal_definition=sig,
                defaults={
                    "source_scope": BindingSourceScope.SUBMISSION_PAYLOAD,
                    "source_data_path": "",
                    "is_required": True,
                },
            )

    # Delete orphaned signals from a previous FMU upload whose variables
    # no longer appear in the new FMU. CASCADE deletes associated bindings.
    orphaned = SignalDefinition.objects.filter(
        workflow_step=step,
        origin_kind=SignalOriginKind.FMU,
    ).exclude(
        # Keep only signals we just created/updated
        contract_key__in=[ck for ck, _ in seen],
    )

    # Before deleting orphaned signals, preserve assertion targets.
    # Assertions using SET_NULL FK would violate the XOR constraint
    # if all three target fields become empty. Set target_data_path
    # to the contract_key so the assertion remains valid.
    if orphaned.exists():
        from validibot.validations.models import RulesetAssertion

        orphan_ids = list(orphaned.values_list("pk", flat=True))
        affected_assertions = RulesetAssertion.objects.filter(
            target_signal_definition_id__in=orphan_ids,
        )
        for assertion in affected_assertions:
            sig = assertion.target_signal_definition
            if sig:
                assertion.target_data_path = sig.contract_key
                assertion.target_signal_definition = None
                assertion.save(
                    update_fields=["target_data_path", "target_signal_definition"],
                )

    deleted_count, _ = orphaned.delete()
    if deleted_count:
        logger.info(
            "Deleted %d orphaned FMU signal definitions on step %s",
            deleted_count,
            step.pk,
        )


def clear_step_fmu_signals(step: WorkflowStep) -> None:
    """Remove all FMU-origin signal definitions from a step.

    Called when the user removes the FMU from a step. CASCADE deletes
    associated ``StepSignalBinding`` rows.
    """
    SignalDefinition.objects.filter(
        workflow_step=step,
        origin_kind=SignalOriginKind.FMU,
    ).delete()


# ── Internal helpers ─────────────────────────────────────────────────


def _direction_for_causality(causality: str) -> str | None:
    """Map FMU causality to signal direction, or None for unsupported types."""
    if causality == "input":
        return SignalDirection.INPUT
    if causality == "output":
        return SignalDirection.OUTPUT
    return None


def _data_type_for_fmu(value_type: str) -> str:
    """Map FMU value type to signal data type."""
    vt = (value_type or "").lower()
    if vt in {"real", "integer", "enumeration"}:
        return CatalogValueType.NUMBER
    if vt == "boolean":
        return CatalogValueType.BOOLEAN
    if vt == "string":
        return CatalogValueType.STRING
    return CatalogValueType.OBJECT

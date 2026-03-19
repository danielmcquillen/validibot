"""
Sync EnergyPlus template variables to ``SignalDefinition`` and ``StepSignalBinding``.

When an author uploads an IDF template to a workflow step, the template
scanner extracts ``$VARIABLE_NAME`` placeholders. This module creates
the corresponding ``SignalDefinition`` and ``StepSignalBinding`` rows
that downstream features (CEL context, signal display, assertion
targeting) use as the single source of truth for template signals.

This is the EnergyPlus counterpart to :mod:`fmu_signals`. Both follow the
same pattern: scan provider-specific source → create step-owned
``SignalDefinition`` (``origin_kind=TEMPLATE``) + ``StepSignalBinding``
rows. Template variables are always inputs (``direction=INPUT``).

**Reconciliation on template re-upload:** When the author uploads a new
template, variable names may change. The function upserts by
``(workflow_step, contract_key, direction)`` and deletes orphaned signals.
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
from validibot.validations.models import SignalDefinition
from validibot.validations.models import StepSignalBinding
from validibot.validations.signal_metadata.metadata import TemplateSignalMetadata

if TYPE_CHECKING:
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


def sync_step_template_signals(
    step: WorkflowStep,
    template_variables: list[dict[str, Any]],
) -> None:
    """Create or update ``SignalDefinition`` and ``StepSignalBinding`` rows
    for every template variable in an EnergyPlus step.

    All template variables are input signals — they represent values the
    submitter provides to parameterize the IDF template before simulation.

    Args:
        step: The workflow step that owns these signals. Must be saved.
        template_variables: List of variable dicts (from the template
            scanner), each with keys: name, description, default, units,
            variable_type, min_value, max_value, min_exclusive,
            max_exclusive, choices.
    """
    if not step.pk:
        raise ValueError("Step must be saved before syncing template signals.")

    seen: set[str] = set()
    batch_keys: set[str] = set()

    for position, var in enumerate(template_variables):
        name = var.get("name", "")
        if not name:
            continue

        # Template variable names like "U_FACTOR" slugify to "u_factor".
        base_key = slugify(name, separator="_") or "signal"
        contract_key = base_key
        counter = 2
        while contract_key in batch_keys:
            contract_key = f"{base_key}_{counter}"
            counter += 1
        batch_keys.add(contract_key)
        seen.add(contract_key)

        variable_type = var.get("variable_type", "text")
        data_type = _data_type_for_template_var(variable_type)

        # Build template-specific metadata for UI rendering and validation.
        metadata = TemplateSignalMetadata(
            variable_type=variable_type,
            min_value=var.get("min_value"),
            min_exclusive=var.get("min_exclusive", False),
            max_value=var.get("max_value"),
            max_exclusive=var.get("max_exclusive", False),
            choices=var.get("choices", []),
        ).model_dump()

        sig, _created = SignalDefinition.objects.update_or_create(
            workflow_step=step,
            contract_key=contract_key,
            direction=SignalDirection.INPUT,
            defaults={
                "native_name": name,
                "label": var.get("description") or "",
                "description": "",
                "data_type": data_type,
                "unit": var.get("units") or "",
                "order": position,
                "origin_kind": SignalOriginKind.TEMPLATE,
                "provider_binding": {
                    "variable_type": variable_type,
                },
                "metadata": metadata,
            },
        )

        # Default value from the template variable config becomes the
        # binding's default_value — used when the submitter omits the
        # variable from their JSON payload.
        default = var.get("default")
        StepSignalBinding.objects.update_or_create(
            workflow_step=step,
            signal_definition=sig,
            defaults={
                "source_scope": BindingSourceScope.SUBMISSION_PAYLOAD,
                "source_data_path": name,
                "default_value": default,
                "is_required": default is None or default == "",
            },
        )

    # Delete orphaned template signals from a previous template upload.
    orphaned = SignalDefinition.objects.filter(
        workflow_step=step,
        origin_kind=SignalOriginKind.TEMPLATE,
    ).exclude(
        contract_key__in=seen,
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
            "Deleted %d orphaned template signal definitions on step %s",
            deleted_count,
            step.pk,
        )


def clear_step_template_signals(step: WorkflowStep) -> None:
    """Remove all template-origin signal definitions from a step.

    Called when the author removes the template or switches to direct mode.
    """
    SignalDefinition.objects.filter(
        workflow_step=step,
        origin_kind=SignalOriginKind.TEMPLATE,
    ).delete()


# ── Internal helpers ─────────────────────────────────────────────────


def _data_type_for_template_var(variable_type: str) -> str:
    """Map template variable type to signal data type."""
    if variable_type == "number":
        return CatalogValueType.NUMBER
    if variable_type == "choice":
        return CatalogValueType.STRING
    return CatalogValueType.STRING

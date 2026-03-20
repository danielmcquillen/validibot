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

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.models import SignalDefinition
from validibot.validations.models import StepSignalBinding

if TYPE_CHECKING:
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


def ensure_step_signal_bindings(step: WorkflowStep) -> int:
    """Create default StepSignalBinding rows for validator-owned input signals.

    For each input SignalDefinition owned by the step's validator that
    doesn't already have a binding on this step, creates a binding with:

    - source_scope: derived from the signal's provider_binding config
      (defaults to SUBMISSION_PAYLOAD)
    - source_data_path: the signal's native_name
    - is_required: True
    - default_value: None

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
    for sig in validator_input_signals:
        if sig.pk in existing_signal_ids:
            continue

        # Derive source_scope from the signal's provider_binding config
        # if it specifies one; otherwise default to SUBMISSION_PAYLOAD.
        provider_binding = sig.provider_binding or {}
        source_scope = provider_binding.get(
            "source_scope",
            BindingSourceScope.SUBMISSION_PAYLOAD,
        )

        StepSignalBinding.objects.create(
            workflow_step=step,
            signal_definition=sig,
            source_scope=source_scope,
            source_data_path=sig.native_name,
            is_required=True,
            default_value=None,
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

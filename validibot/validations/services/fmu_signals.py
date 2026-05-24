"""
Sync step-level FMU variables to ``StepIODefinition`` and ``StepInputBinding``.

When a user uploads an FMU to a workflow step, ``introspect_fmu()`` extracts
the model's input/output variables. This module creates the corresponding
``StepIODefinition`` and ``StepInputBinding`` rows that downstream features
(CEL context, signal display, assertion targeting) use as the single source
of truth for FMU signals.

The core function :func:`sync_step_fmu_signals` is called after ``step.save()``
during FMU upload, and can also be called from data migrations to backfill
existing steps.

**Reconciliation on re-upload:** When a user uploads a different FMU to the
same step, variable names may change. The function upserts by
``(workflow_step, contract_key, direction)`` and deletes orphaned signals.
Cascade-delete on ``StepInputBinding`` ensures bindings are cleaned up too.
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
from validibot.validations.models import StepInputBinding
from validibot.validations.models import StepIODefinition
from validibot.validations.signal_metadata.metadata import FMUProviderBinding
from validibot.validations.signal_metadata.metadata import FMUSignalMetadata

if TYPE_CHECKING:
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


def sync_step_fmu_signals(
    step: WorkflowStep,
    fmu_variables: list[dict[str, Any]],
) -> None:
    """Create or update ``StepIODefinition`` and ``StepInputBinding`` rows
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
    Stable ``contract_key`` preservation via FMI ``value_reference``
    matching is planned follow-up; until then, renamed variables are
    treated as new signals.

    Args:
        step: The workflow step that owns these signals. Must be saved
            (have a PK) before calling.
        fmu_variables: List of variable dicts (from FMU introspection),
            each with keys: name, causality, variability, value_reference,
            value_type, unit, description, label.
    """
    if not step.pk:
        raise ValueError("Step must be saved before syncing FMU signals.")

    # ``seen`` tracks every (contract_key, direction) tuple we touched
    # in this call (parser facts + variables). Two roles:
    #   1. In-batch collision detection. Two variables slugifying to
    #      the same key with the same direction get suffixed (-2, -3, …).
    #      Cross-direction collisions are allowed by the model's
    #      (workflow_step, contract_key, direction) uniqueness.
    #   2. Tuple-aware orphan cleanup at the end. A row whose tuple
    #      isn't in ``seen`` corresponds to a variable that's gone.
    #
    # We deliberately do NOT check pre-existing DB keys for collisions:
    # ``update_or_create`` keyed on (workflow_step, contract_key,
    # direction) reuses the existing row when those match, preserving
    # StepIODefinition.pk so StepInputBinding / WorkflowStepIOPromotion /
    # RulesetAssertion FKs survive re-upload (identity stability —
    # May 2026 review's P1 finding).
    seen: set[tuple[str, str]] = set()

    # Seed parser-fact StepIODefinition rows (Phase 6 / May 2026 P1
    # finding). These mirror the static catalog entries on the system
    # FMU validator so authors get identical i.* resolution whether
    # they bind a workflow step to (a) the system FMU validator + a
    # step-level FMU upload, or (b) a user-created library FMU
    # validator. Without this branch, step-level FMU steps would have
    # an empty i.* even though the system catalog declares the parser
    # facts.
    seed_step_parser_fact_signals(step, seen=seen)

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
        # Suffix only when THIS (key, direction) has already been
        # claimed in this batch. Pre-existing DB rows with the same
        # tuple are reused by update_or_create, not blocked here.
        while (contract_key, direction) in seen:
            contract_key = f"{base_key}_{counter}"
            counter += 1
        seen.add((contract_key, direction))

        # Upsert StepIODefinition
        sig, _created = StepIODefinition.objects.update_or_create(
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

        # Ensure a StepInputBinding exists for each input signal.
        # ``get_or_create`` (not ``update_or_create``) is deliberate:
        # ``source_data_path``, ``source_scope``, and ``is_required``
        # are AUTHOR STATE — the workflow author chooses how each
        # input gets resolved at runtime. On re-upload of an unchanged
        # FMU variable, ``update_or_create(defaults={"source_data_path":
        # "", ...})`` would silently reset a hand-mapped path back to
        # empty string (the May 2026 review's P1 finding caught
        # exactly this). With ``get_or_create``, the defaults apply
        # only when the binding is being created for the first time;
        # existing bindings keep whatever the author has put in.
        #
        # This matches the contract that
        # ``services.signal_bindings.ensure_step_signal_bindings``
        # uses for catalog-driven bindings — create when missing,
        # never overwrite author state.
        if direction == SignalDirection.INPUT:
            StepInputBinding.objects.get_or_create(
                workflow_step=step,
                signal_definition=sig,
                defaults={
                    "source_scope": BindingSourceScope.SUBMISSION_PAYLOAD,
                    "source_data_path": "",
                    "is_required": True,
                },
            )

    # ── Orphan cleanup (tuple-aware) ─────────────────────────────────
    # Delete StepIODefinition rows whose (contract_key, direction) tuple
    # didn't appear in this call. Tuple-aware filtering matters because
    # the model's uniqueness is (workflow_step, contract_key, direction)
    # — the same contract_key can coexist across INPUT and OUTPUT (e.g.,
    # an FMU variable named ``T`` with causality=input and another
    # ``T`` with causality=output). Filtering by contract_key alone
    # would either over-delete (drop the surviving direction) or
    # under-delete (miss a row whose key matches a survivor but
    # direction doesn't).
    #
    # The queryset is small (one step) so the O(n) Python walk is
    # fine. Same pattern as ``services.fmu._persist_variables``.
    candidates = StepIODefinition.objects.filter(
        workflow_step=step,
        origin_kind=SignalOriginKind.FMU,
    )
    orphan_ids = [
        sig.pk for sig in candidates if (sig.contract_key, sig.direction) not in seen
    ]

    # Before deleting orphaned signals, preserve assertion targets.
    # Assertions using SET_NULL FK would violate the XOR constraint
    # if all three target fields become empty. Set target_data_path
    # to the contract_key so the assertion remains valid.
    if orphan_ids:
        from validibot.validations.models import RulesetAssertion

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

    deleted_count, _ = candidates.filter(pk__in=orphan_ids).delete()
    if deleted_count:
        logger.info(
            "Deleted %d orphaned FMU signal definitions on step %s",
            deleted_count,
            step.pk,
        )


def clear_step_fmu_signals(step: WorkflowStep) -> None:
    """Remove all FMU-origin signal definitions from a step.

    Called when the user removes the FMU from a step. CASCADE deletes
    associated ``StepInputBinding`` rows.

    Includes parser-fact rows (origin_kind=FMU, source_kind=INTERNAL)
    seeded by ``seed_step_parser_fact_signals`` because they carry
    the same origin_kind — removing the FMU should remove every
    FMU-derived step input, parser facts included.
    """
    StepIODefinition.objects.filter(
        workflow_step=step,
        origin_kind=SignalOriginKind.FMU,
    ).delete()


def seed_step_parser_fact_signals(
    step: WorkflowStep,
    *,
    seen: set[tuple[str, str]],
) -> None:
    """Seed parser-fact StepIODefinition rows on a step-level FMU upload.

    Step-level counterpart to
    ``services.fmu._seed_parser_fact_signals`` (which handles library
    FMU validators). Both call sites consume the same
    ``PARSER_FACT_SPECS`` / ``_parser_fact_step_io_defaults`` so the
    rows are identical regardless of which path was used — the May
    2026 review's P2 finding caught that mismatch otherwise.

    Identity-stable via ``update_or_create`` keyed on
    ``(workflow_step, contract_key, direction)``: re-uploading an FMU
    reuses the existing parser-fact rows rather than recreating them,
    preserving any author-built ``StepInputBinding`` or
    ``WorkflowStepIOPromotion`` FK relationships.

    The ``seen`` set (mutated in-place) records the
    (contract_key, INPUT) tuples we claimed, so the caller's
    per-variable upsert can detect in-batch collisions and the
    orphan-cleanup at the end can skip parser-fact rows.
    """
    from validibot.validations.services.fmu import PARSER_FACT_SPECS
    from validibot.validations.services.fmu import _parser_fact_step_io_defaults

    for spec in PARSER_FACT_SPECS:
        StepIODefinition.objects.update_or_create(
            workflow_step=step,
            contract_key=spec.contract_key,
            direction=SignalDirection.INPUT,
            defaults=_parser_fact_step_io_defaults(spec),
        )
        seen.add((spec.contract_key, SignalDirection.INPUT))


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

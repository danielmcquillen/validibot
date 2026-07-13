"""The single community projection of a workflow's *validation contract*.

ADR-2026-06-18 (implementation note). This module is the one place that answers
"what, semantically, is this workflow?" for hashing and evidence. It exists so
the community evidence manifest and the Pro signed credential can derive from a
**single** projection rather than two parallel preimages that drift.

Two layers, deliberately named separately (see the ADR's "keep the two layers
distinct" caution):

* :func:`build_workflow_definition_contract` — the **hash preimage**: the
  minimal projection of validation *semantics* (identity, constants, signal
  mapping definitions, and per-step validator + effective-ruleset + assertions).
  This is the only input to the hash.
* :func:`compute_workflow_definition_hash` — ``sha256`` over RFC 8785 / JCS
  canonical JSON for that preimage.
* :func:`build_workflow_contract_snapshot` — the broader **evidence object**: a
  ``validibot_shared.evidence.WorkflowContractSnapshot`` carrying the launch
  contract PLUS the constants, signal-mapping definitions, and the definition
  hash. Both the community evidence manifest (``validations/services/evidence.py``)
  and the Pro signed credential derive from these functions, so they cannot
  drift — a constant value change moves ``compute_workflow_definition_hash`` and
  therefore both the manifest record and the signed credential.

Design rules encoded here (each is an ADR decision):

* **Semantic/cosmetic boundary is defined once.** The assertion projection hashes
  exactly ``RulesetAssertion.IMMUTABLE_ASSERTION_FIELDS`` (plus a *normalized*
  target) — the same allowlist that governs edit-after-runs immutability — so
  "what you can't edit on a locked workflow" and "what's in the hash" cannot
  drift. Cosmetic fields (``order``, ``message_template``, ``success_message``)
  are excluded.
* **Import-stable sort + target.** Constants and signal mappings sort by
  ``name`` (never ``position`` — cosmetic — or ``pk`` — unstable across
  export/import). Assertion targets normalize to the signal ``contract_key`` or
  the free-form path, never the ``target_signal_definition_id`` pk.
* **Constants store exact values.** A ``NUMBER`` constant's decimal string
  (``"0.40"``) is hashed verbatim, preserving attested precision.

Canonicalization: this module hashes with the shared RFC 8785 / JCS helper in
``validibot-shared``. Pro re-exports that helper for compatibility, but the
community projection owns the workflow-definition hash so the evidence manifest
and signed credential cannot drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from validibot_shared.canonicalization import canonicalize_dict
from validibot_shared.canonicalization import sha256_hex_for_dict

if TYPE_CHECKING:
    from validibot_shared.evidence import WorkflowContractSnapshot

    from validibot.validations.models import Ruleset
    from validibot.validations.models import RulesetAssertion
    from validibot.workflows.models import Workflow
    from validibot.workflows.models import WorkflowStep


def build_workflow_definition_contract(workflow: Workflow) -> dict[str, Any]:
    """Return the semantic hash preimage for a workflow.

    Only validation semantics are included — anything an author can edit on a
    locked workflow (cosmetic labels, descriptions, display order) is excluded,
    so a cosmetic change can never move the hash.
    """
    return {
        "workflow_uuid": str(workflow.uuid),
        "constants": _project_constants(workflow),
        "signal_mappings": _project_signal_mappings(workflow),
        "validation_steps": _project_validation_steps(workflow),
    }


def compute_workflow_definition_hash(workflow: Workflow) -> str:
    """Return ``sha256:<hex>`` over the JCS bytes of the definition contract."""
    return _hash_preimage(build_workflow_definition_contract(workflow))


def _hash_preimage(preimage: dict[str, Any]) -> str:
    """Return ``sha256:<hex>`` of a preimage's RFC 8785 / JCS bytes."""
    return f"sha256:{sha256_hex_for_dict(preimage)}"


def build_workflow_contract_snapshot(workflow: Workflow) -> WorkflowContractSnapshot:
    """Build the full evidence ``WorkflowContractSnapshot`` for a workflow.

    This is the **single** producer of the manifest's contract snapshot: it
    carries the launch-contract fields (file types, retention, agent policy)
    *plus* the constants, signal-mapping definitions, and the workflow-definition
    hash (ADR-2026-06-18). The community evidence manifest and the Pro signed
    credential both derive from here, so a constant value change moves the hash
    in both — no drift.
    """
    from validibot_shared.evidence import ContractConstant
    from validibot_shared.evidence import ContractSignalMapping
    from validibot_shared.evidence import WorkflowContractSnapshot

    definition = build_workflow_definition_contract(workflow)
    return WorkflowContractSnapshot(
        allowed_file_types=list(workflow.allowed_file_types or []),
        input_retention=workflow.input_retention or "",
        output_retention=workflow.output_retention or "",
        agent_billing_mode=workflow.agent_billing_mode or "",
        agent_price_cents=workflow.agent_price_cents,
        agent_max_launches_per_hour=workflow.agent_max_launches_per_hour,
        # Shared-schema field names still use pre-rename terminology and map 1:1
        # to the renamed workflow fields (agent_public_discovery == x402_enabled,
        # agent_access_enabled == mcp_enabled). Renaming the shared schema is a
        # separate follow-up; the recorded meaning is unchanged.
        agent_public_discovery=workflow.x402_enabled,
        agent_access_enabled=workflow.mcp_enabled,
        constants=[ContractConstant(**c) for c in definition["constants"]],
        signal_mappings=[
            ContractSignalMapping(**m) for m in definition["signal_mappings"]
        ],
        workflow_definition_hash=_hash_preimage(definition),
    )


# ── Canonicalization ─────────────────────────────────────────────────────────


def _canonical_bytes(data: dict[str, Any]) -> bytes:
    """Serialize to shared RFC 8785 / JCS bytes for deterministic sorting."""
    return canonicalize_dict(data)


# ── Projections ──────────────────────────────────────────────────────────────


def _project_constants(workflow: Workflow) -> list[dict[str, Any]]:
    """Project constants, sorted by name (import-stable), value verbatim."""
    return [
        {
            "name": constant.name,
            "data_type": constant.data_type,
            "value": constant.value,
        }
        for constant in workflow.constants.all().order_by("name")
    ]


def _project_signal_mappings(workflow: Workflow) -> list[dict[str, Any]]:
    """Project signal-mapping *definitions* (never resolved ``s.*`` values).

    Sorted by name. Includes only the workflow-defined config — the resolved
    value is submission-derived and belongs in the retention-gated run record,
    never in the always-publishable contract.
    """
    return [
        {
            "name": mapping.name,
            "source_path": mapping.source_path,
            "on_missing": mapping.on_missing,
            "default_value": mapping.default_value,
            "data_type": mapping.data_type,
        }
        for mapping in workflow.signal_mappings.all().order_by("name")
    ]


def _project_validation_steps(workflow: Workflow) -> list[dict[str, Any]]:
    """Project validation steps in order (action steps excluded)."""
    steps = (
        workflow.steps.select_related("validator", "ruleset")
        .filter(action__isnull=True, validator__isnull=False)
        .order_by("order")
    )
    return [_project_step(step) for step in steps]


def _project_step(step: WorkflowStep) -> dict[str, Any]:
    """Project one validation step to its semantic fields.

    ``step_config`` is hashed **wholesale** — correct by construction now that
    the ``config`` / ``display_settings`` split has landed (ADR-2026-06-18): the
    semantic config Pydantic models use ``extra="forbid"``
    (``workflows/step_configs.py``), so ``config`` can hold only
    validation-affecting keys. Cosmetic labels/previews/counts and the keys the
    runner injects at launch (``primary_file_uri`` …) live in the step's separate
    ``display_settings`` field, which is deliberately NOT part of this preimage.
    """
    validator = step.validator
    return {
        "order": step.order,
        "step_key": step.step_key or "",
        "validator_slug": validator.slug,
        "validator_version": validator.version or "",
        "step_config": step.config or {},
        "assertions": _project_ruleset(_effective_ruleset(step)),
    }


def _effective_ruleset(step: WorkflowStep) -> Ruleset | None:
    """Return the step-level ruleset if present, else the validator default."""
    if step.ruleset_id:
        return step.ruleset
    validator = step.validator
    return getattr(validator, "default_ruleset", None) if validator else None


def _project_ruleset(ruleset: Ruleset | None) -> list[dict[str, Any]]:
    """Project a ruleset's assertions, each to the immutable field set.

    Order is normalized away (sorted by the projected content) so a display
    reorder does not move the hash — only the *set* of semantic assertions
    matters.
    """
    if ruleset is None:
        return []
    projected = [
        _project_assertion(assertion) for assertion in ruleset.assertions.all()
    ]
    # Sort by canonical content so assertion display-order never affects the hash.
    projected.sort(key=_canonical_bytes)
    return projected


def _project_assertion(assertion: RulesetAssertion) -> dict[str, Any]:
    """Project one assertion to EXACTLY ``IMMUTABLE_ASSERTION_FIELDS``.

    Consumes the same allowlist that governs edit-after-runs immutability, so
    the semantic/cosmetic boundary is defined once. The target is normalized to
    the signal ``contract_key`` or the free-form path — never the
    ``target_signal_definition_id`` pk, which is unstable across export/import.
    """
    entry: dict[str, Any] = {}
    for field in assertion.IMMUTABLE_ASSERTION_FIELDS:
        if field == "target_signal_definition_id":
            # Normalize the target to a stable, import-safe identifier.
            entry["target_field"] = _normalized_target(assertion)
        elif field == "target_data_path":
            continue  # folded into target_field above
        else:
            entry[field] = getattr(assertion, field, None)
    return entry


def _normalized_target(assertion: RulesetAssertion) -> str:
    """Resolve an assertion's target to a stable key (contract_key or path)."""
    if assertion.target_signal_definition_id and assertion.target_signal_definition:
        return assertion.target_signal_definition.contract_key
    return assertion.target_data_path or ""

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
* :func:`compute_workflow_definition_hash` — ``sha256`` over the canonical JSON
  of that preimage.

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

Release-gated remainder (NOT done here, needs a ``validibot-shared`` release +
the access-control refactor's sequencing — see ADR):

* Adding ``constants`` / ``signal_mappings`` / ``workflow_definition_hash`` as
  optional fields on ``validibot_shared.evidence.WorkflowContractSnapshot``
  (stays ``v1``; additive) and populating them in the manifest builder.
* Pro's ``credentials/workflow_digest.py`` delegating to this module instead of
  building its own preimage, and the JCS canonicalizer moving to
  ``validibot-shared`` so both use identical bytes.

Until that reconciliation, this module canonicalizes with sorted-key JSON (the
same scheme the manifest *envelope* already uses). The projection **logic** —
field set, ordering, semantic boundary — is final; only the canonicalizer swaps
to RFC 8785 / JCS at reconciliation, and nothing consumes this hash yet, so no
stored value is invalidated.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
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
    """Return ``sha256:<hex>`` over the canonical JSON of the definition contract.

    See the module docstring for why the canonicalizer is sorted-key JSON today
    and reconciles to JCS at release time.
    """
    preimage = build_workflow_definition_contract(workflow)
    digest = hashlib.sha256(_canonical_bytes(preimage)).hexdigest()
    return f"sha256:{digest}"


# ── Canonicalization ─────────────────────────────────────────────────────────


def _canonical_bytes(data: dict[str, Any]) -> bytes:
    """Serialize to canonical JSON bytes (sorted keys, compact separators).

    Hash stability comes from this canonical form, NOT from the in-code field
    ordering above (which is for readability). ``default=str`` defends against a
    stray non-JSON scalar (e.g. a ``Decimal``) rather than raising.
    """
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


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

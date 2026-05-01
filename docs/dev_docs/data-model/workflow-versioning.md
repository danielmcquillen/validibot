# Workflow versioning and the trust contract

This page documents the *trust model* behind workflow versioning: what
counts as a "launch contract", how the platform proves that contract is
immutable once a run has happened, and what to do about workflows that
predate this enforcement.

The work was delivered in [ADR-2026-04-27 "Trust-boundary hardening
and evidence-first validation"][adr], specifically Phase 3 (Sessions
A-D). This page is the developer-facing companion: it summarises what
shipped, how to extend it, and how to run the auditor in production.

[adr]: https://github.com/danielmcquillen/validibot-project/blob/main/docs/adr/2026-04-27-trust-boundary-hardening-and-evidence-first-validation.md

## Why trust matters here

A validation run is a fact: "submission X passed workflow Y at time T".
For that fact to mean anything in the future, the workflow's *rules at
time T* must remain pinned. If we silently let workflow Y change its
rules in place, every previously-claimed pass becomes
non-reproducible — and any artefact (PDF report, signed credential,
external API response) that referenced "validated by Y" loses its
ground truth.

The trust contract in our model is the set of fields and dependent
rows that determine *what gets checked* when a workflow runs. We
enforce three properties:

1. **The workflow row's contract fields are immutable** once the
   workflow has runs (or is locked) — operators must clone to a new
   version.
2. **The validator a step uses is immutable** under the same `(slug,
   version)` — bumping the config's behavior requires a version bump,
   so old workflows stay pinned to the old validator row.
3. **The rules and resources a step depends on are immutable** —
   rulesets, assertions, and uploaded files cannot silently mutate
   under a locked workflow.

## Where the gates live

| Concern | Field of truth | Where the gate is enforced |
|---|---|---|
| Workflow contract fields | `Workflow.allowed_file_types`, `data_retention`, `output_retention`, `agent_*` | `WorkflowForm.clean()` rejects edits via `Workflow.changed_contract_fields()` |
| Validator semantic config | `Validator.semantic_digest` (SHA-256) | `sync_validators` raises `CommandError` on mismatch under the same `(slug, version)`; `--allow-drift` for dev override |
| Validator class identity | `Validator.slug` + `Validator.version` (unique constraint `uq_validator_slug_version`) | `sync_validators` keys by `(slug, version)`; bumping `version` creates a new row |
| Ruleset rules | `Ruleset.rules_text`, `rules_file`, `metadata`, `ruleset_type` | `Ruleset.clean()` rejects mutation when `is_used_by_locked_workflow()` is true |
| Ruleset assertions | `RulesetAssertion.operator`, `target`, `rhs`, `options`, `when_expression`, `severity`, `spec_version`, `assertion_type` | `RulesetAssertion.clean()` rejects mutation AND rejects adding new rows when parent is in use |
| Catalog file content | `ValidatorResourceFile.content_hash` (SHA-256) | `ValidatorResourceFile.save()` raises if hash differs and the row is referenced by a locked workflow |
| Step-owned file content | `WorkflowStepResource.content_hash` (SHA-256) | `WorkflowStepResource.save()` raises if hash differs and the step's workflow is locked |

The unifying pattern is **"`is_used_by_locked_workflow()` + diff
detection in `clean()` or `save()`"**. Both `Workflow.has_runs()` and
`Workflow.is_locked` count as "in use" — once a contract has been
exercised by a real run or explicitly committed via locking, mutation
is rejected.

## Why this is a *gate*, not a check

The gates raise at write time. They do not run after the fact. A
hand-edit of the database, a `Model.objects.update(...)` query, or a
script that calls `super().save()` directly will all bypass the gate
and silently mutate. **This is intentional**: defending against
adversarial operators is out of scope; the goal is to catch *honest*
mistakes (and require a deliberate hand to bypass).

The follow-up safety net is the auditor described below.

## Legacy versioning

Two situations leave a workflow legacy-versioned:

- **Pre-ADR rows.** Workflows that were locked or had runs before
  Sessions B and C deployed don't have populated `semantic_digest` or
  `content_hash` columns. Their rules might be perfectly stable, but
  we can't *prove* it from the trust columns alone.
- **Custom validators.** Org-owned validators (`Validator.is_system =
  False`) are created via the admin UI, not via `sync_validators`.
  Their `semantic_digest` stays empty by design — there's no config
  to compare against.

Legacy-versioning is not broken; it's just opaque. A locked workflow
on a legacy validator may behave perfectly consistently — but if
something *did* drift, the gate wouldn't catch it because it has no
baseline to compare against.

## The audit command

Run from any management shell:

```bash
python manage.py audit_workflow_versions
```

By default, the audit walks every "in-use" workflow (locked OR has at
least one validation run) and reports findings per workflow. Each
finding has a code, a severity, and a human-readable message:

- `VALIDATOR_DIGEST_MISSING` — the step's validator has no digest.
  Severity `info` for locked-but-unrun workflows; `warn` for workflows
  with actual runs.
- `VALIDATOR_DIGEST_DRIFT` — the validator's stored digest disagrees
  with what the current config would compute. Severity `error`.
  Indicates someone bypassed Session B's gate (e.g. used
  `--allow-drift` then forgot to follow up, or hand-edited a row).
- `CATALOG_RESOURCE_HASH_MISSING` — a `ValidatorResourceFile` referenced
  by a step has no `content_hash`. Severity `info` / `warn` per the
  workflow's run state.
- `STEP_RESOURCE_HASH_MISSING` — a step-owned `WorkflowStepResource`
  has no `content_hash`. Severity `info` / `warn`.
- `STEP_RESOURCE_HASH_DRIFT` — the step-owned file's stored
  `content_hash` doesn't match the current bytes hash. Severity
  `error`. Indicates someone replaced bytes outside the gate (raw
  filesystem write, manual GCS upload, etc.).
- `STEP_RESOURCE_READ_ERROR` — the file couldn't be read at audit
  time. Severity `warn`. Suggests storage misconfiguration; the drift
  check couldn't run.
- `MANIFEST_MISSING` — a completed run (terminal status) has no
  `RunEvidenceArtifact` row. Either the run finished before Phase 4
  Session A's manifest stamper deployed, or stamping silently failed
  before the FAILED row could be recorded. Severity `warn`.
- `MANIFEST_GENERATION_FAILED` — a run has a `RunEvidenceArtifact`
  in `availability=FAILED` state. The `generation_error` column
  records why. Severity `error`.

### Useful flags

- `--include-unused` — also audit fresh workflows (those without runs
  and not locked). Useful before locking a batch.
- `--workflow-id <pk>` — audit a single workflow.
- `--strict` — `warn`-level findings exit non-zero. Suitable for CI
  gates that want to block any legacy versioning.
- `--json` — emit a structured report against the
  `validibot.workflow_audit.v1` schema. Suitable for piping into
  dashboards.

### Exit codes

- `0` — no findings, or only `info` / `warn` findings (without
  `--strict`).
- `1` — at least one `error` finding, OR at least one `warn` finding
  with `--strict`.

### Recommended deploy hooks

In CI: `python manage.py audit_workflow_versions --strict --json` as a
post-deploy check. Block the rollout if anything but `info` shows up.

In production: schedule a daily `audit_workflow_versions --json`
that pipes into your observability pipeline. `error` findings page;
`warn` findings open a ticket.

## What to do about legacy findings

| Finding | Remediation |
|---|---|
| `VALIDATOR_DIGEST_MISSING` (system validator) | Run `sync_validators` against the deployment. The first sync after Session B populates the digest. |
| `VALIDATOR_DIGEST_MISSING` (custom validator) | No automated remediation. Document that this workflow uses a custom validator and accept legacy versioning, or migrate the rules into a system validator. |
| `VALIDATOR_DIGEST_DRIFT` | Investigate: someone bypassed Session B's gate. Either bump the validator's `version` (creating a new row that locks the new behavior) or fix the underlying mutation and re-sync. |
| `CATALOG_RESOURCE_HASH_MISSING` | Re-save the `ValidatorResourceFile` row (e.g. via the admin). The save triggers `content_hash` population. |
| `STEP_RESOURCE_HASH_MISSING` | Re-save the `WorkflowStepResource` (often by editing the parent step). |
| `STEP_RESOURCE_HASH_DRIFT` | Same as `VALIDATOR_DIGEST_DRIFT`: investigate the source of the bytes change. The workflow's launch contract is provably broken; the workflow should be cloned to a new version with the corrected file before any new runs land on it. |
| `MANIFEST_MISSING` | Re-finalise the run via the admin or a management script — that triggers the manifest stamper and the row appears. For very old runs (years) where the original workflow has been mutated since, accept legacy versioning and document. |
| `MANIFEST_GENERATION_FAILED` | Read `RunEvidenceArtifact.generation_error` on the row. Common causes: storage backend unreachable, schema validation failure (rare bug). Fix the underlying issue and re-stamp via `EvidenceManifestBuilder.persist(run, EvidenceManifestBuilder.build(run))`. |

## Adding a new contract field

When a future ADR introduces a new field that should be part of the
launch contract:

1. Add it to `Workflow` model.
2. Add it to `validibot.workflows.services.versioning.CONTRACT_FIELDS`.
3. Make sure `WorkflowVersioningService.clone()` copies it.
4. Add a test in `test_versioning.py` that checks the new field is
   copied verbatim.

The contract gate (`WorkflowForm.clean()`) automatically picks up the
new field because it iterates `CONTRACT_FIELDS`. No form change needed.

## Adding a new immutable validator field

Future ADR adds a behavior-defining field to `ValidatorConfig`:

1. Add the field to the Pydantic model and the `Validator` row.
2. Add the field name to
   `validibot.validations.services.validator_digest.SEMANTIC_FIELDS`.
3. Run `sync_validators --allow-drift` once on each deployment to
   re-populate digests; CI will then enforce on the new field.

## Evidence manifests (Phase 4 Session A)

A completed run also gets a *manifest* — a canonical-JSON document
that snapshots "what rules and inputs run X was operating under."
The manifest is hashed, written to default storage, and indexed by a
`RunEvidenceArtifact` row pointing at the file.

The schema is `validibot.evidence.v1` (see
`validibot_shared.evidence` in the published `validibot-shared`
package — version 0.5.0+). It lives in shared so external verifiers
(validibot-pro, third-party tools) can consume it without pulling in
the Django stack. For Session A it includes:

- Run identity: run UUID, workflow slug + version, org, executed at.
- Workflow contract snapshot: every field in `CONTRACT_FIELDS` at the
  moment the run completed.
- Per-step validator records: slug, version, and `semantic_digest`
  pulled directly from each step's validator row.
- Input schema: the workflow's structured input contract if any.
- Retention class: a placeholder for Session B's redaction policy.
- Payload digests: `None` for now; Session B fills in input/output
  hashes including the always-on input hash that survives
  `DO_NOT_STORE` purges.

The stamper lives at
`validibot/validations/services/evidence.py`. Both run-completion
paths (`step_orchestrator.execute_workflow_steps` for sync runs and
`validation_callback._finalise_run_for_status` for async) call
`stamp_evidence_manifest(run)`. The function is best-effort: any
exception is caught, logged, recorded as
`availability=FAILED` on the row, and swallowed so the run's outcome
is unaffected. The auditor then surfaces the gap.


## Related ADRs

- [ADR-2026-04-27 — Trust-boundary hardening and evidence-first
  validation][adr]: the whole story.
- ADR-2026-03-04 — EnergyPlus parameterized model templates: defines
  the `WorkflowStepResource` shape that Session C added hashing to.

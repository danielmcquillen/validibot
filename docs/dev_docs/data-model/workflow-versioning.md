# Workflow versioning and the trust contract

This page documents the *trust model* behind workflow versioning: what
counts as a "launch contract", how the platform proves that contract is
immutable once a run has happened, and what to do about workflows that
predate this enforcement.

This is the developer-facing reference: it summarises how the trust
contract is enforced, how to extend it, and how to run the auditor in
production.

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

1. **Versioned workflow contract fields are protected** once the
   workflow has runs (or is locked). Operators must clone to a new
   version for unsafe semantic edits.
2. **The validator a step uses is immutable** under the same `(slug,
   version)` - bumping the config's behavior requires a version bump,
   so old workflows stay pinned to the old validator row.
3. **The rules and resources a step depends on are immutable** -
   rulesets, assertions, and uploaded files cannot silently mutate
   under a versioned locked/run-having workflow.

**Editing policy** is the author-facing label for
`Workflow.history_policy`, which controls whether a workflow uses this
versioned-history behavior or mutable-history behavior:

- `versioned` (default): unsafe semantic edits are blocked after runs;
  clone the workflow and edit the new version.
- `mutable`: semantic edits may happen in place between runs; old runs are
  records of outcomes, not reproducible evidence against the current
  workflow definition. Until execution snapshots exist, semantic saves are
  temporarily rejected while a run is using the live definition.

History policy can change freely before runs exist. Once a workflow has
runs or is locked, changing history policy itself requires a new workflow
version. This applies in both directions: `mutable -> versioned` would
overstate what old mutable-history runs can prove, and `versioned ->
mutable` would let future edits rewrite the definition older
versioned-history runs point at.

New workflows, older portable definitions that omit `history_policy`, and
test factories default to `versioned`. Mutable is accepted only when the
author or imported definition chooses it explicitly. Invalid imported values
are rejected with `vaf.invalid_history_policy`.

The initial hosted rollout of `ValidationRun.definition_released_at` uses the
clean application-data reset approved for this currently empty installation;
migration `0037` therefore has no heuristic backfill. A populated deployment
must use a reviewed data migration and worker-drain strategy instead.

Workflow versions are family-local identifiers. The database enforces
`uq_workflow_org_slug_version`, so one organization cannot have two rows
with the same workflow `slug` and `version`. Workflow versions are positive
integers (`1`, `2`, `42`). Semver-style labels and ad-hoc labels such as
`latest` are invalid because the version field is an ordering key, not a
release-compatibility promise. Use `parse_workflow_version()` /
`compare_workflow_versions()` from `validibot.workflows.version_utils` for
ordering; do not sort display strings lexicographically.

## Where the gates live

| Concern | Field of truth | Where the gate is enforced |
|---|---|---|
| Editing policy | `Workflow.history_policy` | `editing_policy.validate_editing_policy_transition()` and the transactional settings save block policy changes in either direction when runs exist or the row is locked |
| Workflow contract fields | `Workflow.allowed_file_types`, `input_retention`, `output_retention`, `input_schema` | `editing_policy.guard_workflow_definition_mutation()` serializes semantic saves; Versioned rows use the historical immutability gate and Mutable rows reject while an unreleased run exists |
| Validator semantic config | `Validator.semantic_digest` (SHA-256) | `sync_validators` raises `CommandError` on mismatch under the same `(slug, version)`; `--allow-drift` for dev override |
| Validator class identity | `Validator.slug` + integer `Validator.version` (unique constraint `uq_validator_slug_version`) | `sync_validators` keys by `(slug, version)`; bumping `version` creates a new row |
| Ruleset rules | `Ruleset.rules_text`, `rules_file`, `metadata`, `ruleset_type` | `Ruleset.clean()` rejects mutation when `is_used_by_locked_workflow()` is true |
| Ruleset assertions | `RulesetAssertion.operator`, `target`, `rhs`, `options`, `when_expression`, `severity`, `spec_version`, `assertion_type` | `RulesetAssertion.clean()` rejects mutation AND rejects adding new rows when parent is in use |
| Workflow signal mappings | `WorkflowSignalMapping.name`, `source_path`, `on_missing`, `default_value`, `data_type` | `WorkflowSignalMapping.clean()`/`delete()` reject add/edit/delete when the workflow is versioned + locked/run-having (ADR-2026-06-18 — closes a pre-existing gap) |
| Workflow constants | `WorkflowConstant.name`, `value`, `data_type` | `WorkflowConstant.clean()`/`delete()` reject add/edit/delete under the same gate; cosmetic `description`/`position` stay editable (ADR-2026-06-18) |
| Catalog file content | `ValidatorResourceFile.content_hash` (SHA-256) | `ValidatorResourceFile.save()` raises if hash differs and the row is referenced by a versioned locked/run-having workflow |
| Step-owned file content | `WorkflowStepResource.content_hash` (SHA-256) | `WorkflowStepResource.save()` raises if hash differs and the step's workflow is versioned and locked/run-having |

The unifying active-use boundary is
`validibot.workflows.services.editing_policy.guard_workflow_definition_mutation()`.
Normal model and service mutation seams identify the affected workflow rows,
lock them in primary-key order, and apply one semantic/cosmetic decision.
Existing Versioned form/model gates continue to enforce historical
immutability. Mutable workflows opt out of that historical gate but are blocked
while an exact-workflow run has `definition_released_at IS NULL`. An audited
Versioned superuser repair explicitly joins the same active-use fence. Multi-row
saves hold one transaction, so a rejected mixed edit cannot persist a cosmetic
subset.

## Run admission and release lifecycle

Every production creator calls
`validibot.validations.services.run_admission.admit_validation_run()`.
Admission locks the workflow row first and the submission row second, validates
that both belong to the same organization, and creates a pending run with
`definition_released_at=NULL`. Dispatch happens only after that transaction
commits. The shared Cloud x402 adapter delegates to the same primitive.

Semantic mutation locks the same workflow row and checks the partial-indexed
unreleased-run predicate in its transaction. If mutation commits first, the
next run uses the new definition. If admission commits first, the mutation
returns `workflow_definition_in_use` and persists nothing.

Completion paths call `emit_validation_run_finalized()`. Robust finalization
receivers run while the marker remains null; the helper then releases it in an
idempotent `finally` boundary. Normal completion, callback and dispatch
failure, cancellation, timeout, and stuck-run cleanup use this helper. A
terminal `status` does not release definition use by itself.

This fence intentionally may require a quiet save window on a busy Mutable
workflow. Deactivate the workflow to stop admissions, wait for current runs to
release, save, and reactivate. Immutable per-run execution snapshots are the
planned improvement that can eventually permit concurrent Mutable edits.

## Clone Boundary

`WorkflowVersioningService.clone()` clones the workflow-owned contract
tree. Copying only the top-level workflow row is not enough; child rows
must be independent so edits to the new version cannot mutate historical
meaning on the source version.

Copied rows include:

- the `Workflow` row, including `history_policy`;
- `WorkflowStep` rows and step config;
- step-level `Ruleset` rows;
- `RulesetAssertion` rows attached to cloned rulesets;
- step-owned `StepIODefinition`, `StepInputBinding`, and `Derivation`
  rows;
- `WorkflowStepIOPromotion` overlay rows (workflow-scoped promotions of
  validator-owned step inputs/outputs);
- step-owned `WorkflowStepResource` files;
- `WorkflowPublicInfo`;
- `WorkflowRoleAccess`;
- `WorkflowSignalMapping`;
- `WorkflowConstant` (the `c.*` namespace — carried forward verbatim so a new
  version keeps the fixed thresholds its assertions depend on).

Referenced rows include:

- system and library `Validator` rows, because validators have their own
  `(slug, version)` trust boundary;
- validator-owned step I/O definitions and derivations;
- catalog `ValidatorResourceFile` rows, because content hashes protect
  shared file bytes;
- historical rows such as submissions, validation runs, findings,
  evidence, and artifacts.

## Authoring and API Surfaces

Workflow versions are visible in the authoring UI. The shared
`build_workflow_version_context()` helper in
`validibot.workflows.services.version_context` builds the version-history
context used by workflow detail and other workflow-scoped views.

The workflow detail page includes a **Version history** card for visible
versions in the same `(org, slug)` family. Each row links to that exact
workflow row; primary-key URLs never silently resolve to "latest". The public
workflow directory is different: it groups by `(org, slug)` and shows only the
latest active, non-archived, non-tombstoned version of each family.

The edit form uses two paths:

1. A normal save for edits that are allowed in place.
2. An explicit **Create version and apply** submit when
   `WorkflowForm.requires_new_version_for_save` is set by the
   history/contract gate.

The second path validates the submitted settings against the source row
without enforcing the history lock, clones the workflow with
`WorkflowVersioningService.clone()`, then applies the submitted settings
to the new row inside one transaction. If the apply step fails, the
transaction rolls back and no partial clone remains.

The org-scoped REST API remains read-mostly: clients still cannot create,
patch, or delete workflow definitions directly. The explicit versioning
exception is:

```text
POST /api/v1/orgs/{org_slug}/workflows/{identifier}/clone/
POST /api/v1/orgs/{org_slug}/workflows/{workflow_slug}/versions/{version}/clone/
```

Both routes require workflow-edit permission, clone the resolved source
row, and return the new workflow plus the `CloneReport` payload. The
latest-version route resolves slugs to the latest visible version; the
version-pinned route clones the exact requested version.

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

- **Older rows.** Workflows that were locked or had runs before
  digest/hash enforcement deployed don't have populated `semantic_digest` or
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
  Indicates someone bypassed validator immutability checks (e.g. used
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
  `RunEvidenceArtifact` row. Either the run finished before the
  manifest stamper deployed, or stamping silently failed
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
| `VALIDATOR_DIGEST_MISSING` (system validator) | Run `sync_validators` against the deployment. The first sync after digest enforcement populates the digest. |
| `VALIDATOR_DIGEST_MISSING` (custom validator) | No automated remediation. Document that this workflow uses a custom validator and accept legacy versioning, or migrate the rules into a system validator. |
| `VALIDATOR_DIGEST_DRIFT` | Investigate: someone bypassed validator immutability checks. Either bump the validator's `version` (creating a new row that locks the new behavior) or fix the underlying mutation and re-sync. |
| `CATALOG_RESOURCE_HASH_MISSING` | Re-save the `ValidatorResourceFile` row (e.g. via the admin). The save triggers `content_hash` population. |
| `STEP_RESOURCE_HASH_MISSING` | Re-save the `WorkflowStepResource` (often by editing the parent step). |
| `STEP_RESOURCE_HASH_DRIFT` | Same as `VALIDATOR_DIGEST_DRIFT`: investigate the source of the bytes change. The workflow's launch contract is provably broken; the workflow should be cloned to a new version with the corrected file before any new runs land on it. |
| `MANIFEST_MISSING` | Re-finalise the run via the admin or a management script — that triggers the manifest stamper and the row appears. For very old runs (years) where the original workflow has been mutated since, accept legacy versioning and document. |
| `MANIFEST_GENERATION_FAILED` | Read `RunEvidenceArtifact.generation_error` on the row. Common causes: storage backend unreachable, schema validation failure (rare bug). Fix the underlying issue and re-stamp via `EvidenceManifestBuilder.persist(run, EvidenceManifestBuilder.build(run))`. |

## Adding a new contract field

When a future feature introduces a new field that should be part of the
launch contract:

1. Add it to `Workflow` model.
2. Add it to `validibot.workflows.services.versioning.CONTRACT_FIELDS`.
3. Make sure `WorkflowVersioningService.clone()` copies it.
4. Add the field to the semantic workflow field set in
   `workflows.services.editing_policy` unless it is demonstrably cosmetic.
5. Add tests that prove cloning preserves it and that both Versioned history
   and the Mutable definition-use fence protect it.

The settings form compares `CONTRACT_FIELDS`; child-row and non-form mutation
paths must call the shared guard explicitly. Do not assume a form-only check
protects services, HTMX endpoints, or model writes.

## Adding a new immutable validator field

Future feature adds a behavior-defining field to `ValidatorConfig`:

1. Add the field to the Pydantic model and the `Validator` row.
2. Add the field name to
   `validibot.validations.services.validator_digest.SEMANTIC_FIELDS`.
3. Run `sync_validators --allow-drift` once on each deployment to
   re-populate digests; CI will then enforce on the new field.

## Evidence manifests

A completed run gets a canonical JSON evidence manifest. It is a small
permanent receipt, not a workflow snapshot. The v2 schema resolves at
`https://validibot.com/schemas/evidence-manifest-v2.json` and contains only run
identity/outcome, workflow slug/version, ordered validator-step identity and
status, optional backend image digests, and top-level input/output digests.

It excludes the full workflow contract, accepted-file policy, embedded input
schemas, retention settings, filenames, lineage, provider execution identity,
and raw payloads. See [Evidence Bundles](../overview/evidence-bundles.md) for the
normative field list and verification model.

The stamper lives at
`validibot/validations/services/evidence.py`. Both run-completion
paths (`step_orchestrator.execute_workflow_steps` for sync runs and
`validation_callback._finalise_run_for_status` for async) call
`stamp_evidence_manifest(run)`. The function is best-effort: any
exception is caught, logged, recorded as
`availability=FAILED` on the row, and swallowed so the run's outcome
is unaffected. The auditor then surfaces the gap.

### Retention policy

Input and output retention deletes payload bytes but does not select manifest
fields or delete the permanent receipt. The same v2 shape is emitted for every
retention class. Its hashes remain records about deleted payloads and must not
be described as anonymous data.

### Operator export

The run-detail page exposes a "Download manifest.json" action
backed by `EvidenceManifestDownloadView` at
`validations:evidence_manifest_download`. The endpoint streams the
canonical-JSON bytes that `RunEvidenceArtifact.manifest_path`
points at and includes two receipt-identity headers:

- `X-Validibot-Manifest-Sha256` — the stored manifest hash.
- `X-Validibot-Schema-Version` — the v2 schema URL.

`Cache-Control: no-store` is set so re-stamping a manifest (e.g.
after a builder fix) surfaces fresh bytes on the next download.

Permissions piggyback on the run-detail view. Cross-org and failed-artifact
accesses return `404`. Ordinary input/output expiry does not hide the permanent
manifest.

### Operator export — bundle

A second endpoint at `validations:evidence_bundle_download`
(`<uuid:pk>/evidence/bundle/`) returns the run's evidence as `.tar.gz`:

- `manifest.json` — same canonical bytes the manifest endpoint
  returns; verifiers re-hash this to confirm integrity.
- `credential.jwt` — the compact-JWS signed credential (only when
  `validibot-pro` is installed AND the run has an
  `IssuedCredential`). Carries the
  `credentialSubject.validationRun.manifestHash` claim that binds
  the credential to the manifest's exact bytes.

Pro-aware inclusion: the bundle service uses
`apps.is_installed("validibot_pro")` (mirroring
`get_signed_credential_display_context`) to decide whether to
look up an `IssuedCredential` and include `credential.jwt`. A
community-only deployment produces a bundle without the
signature, no feature flag, no separate code path.

There is no README, bundle descriptor, separate signature file, or raw input or
output member. A Community bundle contains only `manifest.json` and is a record,
not cryptographic proof.

Verification consumes the bundle: parses the JWS in
`credential.jwt`, validates the signature against the issuer's
public key, recomputes SHA-256 of `manifest.json` bytes, and
compares to the credential's `manifestHash` claim.

### Credential workflow definition hash

Signed credentials also include a workflow definition hash in
`credentialSubject.validationRun.workflow.definitionHash`. The hash is
computed at issuance time by
`validibot_pro.credentials.workflow_digest.compute_workflow_definition_hash()`
and persisted in `ValidationCredentialDigestMetadata`.

This is the bridge between workflow history policy and credentials:
even if a mutable workflow changes after issuance, the signed payload
still names the exact definition digest that produced the credential.
Versioned history remains the recommended mode for credential-bearing
workflows because it gives operators a normal workflow row to inspect,
but the credential does not rely on the row staying mutable-state-identical
forever.

## Regression-test map

- `workflows/tests/services/test_editing_policy.py` covers transitions,
  semantic/cosmetic classification, model/service bypass attempts, active-run
  states, idempotent release behavior, and user-facing pluralization.
- `workflows/tests/services/test_editing_policy_concurrency.py` uses separate
  PostgreSQL connections to prove both possible admission/mutation lock orders.
- `validations/tests/services/test_run_admission.py` covers tenant validation,
  admission markers, robust finalization callbacks, and idempotent release.
- `workflows/tests/test_workflow_integration.py` covers the accessible select,
  fixed-state reason, retryable settings error, cosmetic saves, and successful
  retry after release.
- `workflows/tests/test_workflow_io.py` covers explicit Mutable round-trip,
  omitted-field Versioned default, and invalid-value rejection.
- `static/src/ts/features/richTooltips.test.ts` covers idempotent initial and
  HTMX lifecycle behavior for the information affordance.
- `validibot-cloud/validibot_cloud/agents/tests/test_run_creation.py` proves
  hosted x402 admission participates in the same fence.

## See also

- [Workflow import and export](workflow-import-export.md) — the portable-file
  cousin of cloning. It deliberately reuses this page's create-order and
  FK-rebinding rules (rulesets before steps, signals before assertions) so the
  two paths can't drift.

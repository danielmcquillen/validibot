# Evidence Bundles

Every completed validation run can produce an **evidence bundle** — a portable artifact that documents what was checked, against what rules, by whom, with what verdict. This page is the developer-facing reference: schema, generation, retention rules, and how the manifest links to signed credentials.

Workflow-versioning trust columns (validator semantic digests, resource content hashes) and the evidence bundle that wraps them are the operator-facing layer of Validibot's trust model.

## What an evidence bundle proves

A signed credential without evidence is just a stamp. The bundle is what makes the stamp verifiable.

An evidence bundle answers, for one validation run:

- *what was checked* — the workflow contract snapshot;
- *what was checked against* — input bytes, by hash;
- *what the verdict was* — pass/fail/error, finding categories, finding counts;
- *who/what asked* — the trusted source (API, CLI, MCP, x402, web);
- *under what isolation boundary* — validator backend image digest, execution backend, runner;
- *when, in which version*, with stable cryptographic links between every layer.

For the risk-averse customers who choose self-hosting because their model files can't leave their environment, the evidence bundle is the artefact they can hand to a compliance officer, an auditor, a journal reviewer, or a regulator — without sending the model file itself.

## Schema: `validibot.evidence.v1`

The schema lives in `validibot-shared` (Pydantic, no Django dependency) so external verifiers can consume it without installing the full Django stack. Module: `validibot_shared.evidence`. Symbols:

- `EvidenceManifest` — top-level model
- `WorkflowContractSnapshot` — every contract field at run time
- `StepValidatorRecord` — per-step validator slug, version, semantic_digest
- `ManifestRetentionInfo` — retention class plus redactions applied
- `ManifestPayloadDigests` — input/output hashes when policy allows
- `SCHEMA_VERSION` — the literal string `"validibot.evidence.v1"`

Every release that adds *additive* schema fields preserves the v1 schema version. Renaming or removing fields requires a v2 bump.

### What's in the manifest

```json
{
  "schema_version": "validibot.evidence.v1",
  "run": {
    "id": "uuid",
    "status": "FAILED",
    "started_at": "2026-04-27T02:10:00Z",
    "finished_at": "2026-04-27T02:11:30Z",
    "source": "MCP"
  },
  "workflow": {
    "id": "uuid",
    "slug": "energyplus-preflight",
    "version": "3",
    "digest": "sha256:..."
  },
  "submission": {
    "file_type": "text",
    "original_filename": "model.idf",
    "size_bytes": 123456,
    "sha256": "..."
  },
  "validators": [
    {
      "slug": "energyplus",
      "version": "24.2.0",
      "validator_class": "validibot.validations.validators.energyplus.validator.EnergyPlusValidator",
      "advanced": true,
      "validator_backend": {
        "slug": "energyplus",
        "version": "24.2.0",
        "source_repo": "validibot-validator-backends",
        "execution_backend": "DockerComposeExecutionBackend",
        "runner": "DockerValidatorRunner",
        "image": "ghcr.io/validibot/energyplus@sha256:...",
        "metadata_digest": "sha256:...",
        "input_envelope_schema": "validibot.input.v1",
        "output_envelope_schema": "validibot.output.v1"
      }
    }
  ],
  "outputs": [
    {
      "name": "output.json",
      "sha256": "...",
      "content_type": "application/json"
    }
  ],
  "credential": {
    "issued": true,
    "credential_id": "uuid",
    "verification_url": "https://..."
  }
}
```

## Bundle layout (when exported)

When an operator exports a bundle, the artefacts are arranged as:

```text
manifest.json
workflow.json
input/
  hashes.json
results/
  output-envelope.json
  validation-summary.json
logs/
  step-*.log
credentials/
  validation-credential.json
```

`workflow.json` carries the full snapshot of the workflow record at run time so the bundle is self-contained. `logs/` is included only when retention permits; under `DO_NOT_STORE` it's omitted entirely.

## Storage layout

Evidence files live in the configured application storage, not as large database blobs:

```text
<DATA_STORAGE_ROOT>/evidence/<org_id>/<run_id>/
  manifest.json
  bundle.zip              # optional cached export
  bundle.sha256
```

The org-scoped partition is real and gets backed up alongside `runs/`. The backup manifest lists `run-evidence` as a first-class data category alongside `database`, `media`, and `validator-resources`.

## Database index: `RunEvidenceArtifact`

A small index model in `validibot/validations/models.py` — one row per run, one-to-one with `ValidationRun`.

```python
class RunEvidenceArtifact(TimeStampedModel):
    run = OneToOneField(ValidationRun, ...)
    schema_version = CharField(max_length=32)         # "validibot.evidence.v1"
    manifest_path = CharField(max_length=500)          # storage URI
    manifest_hash = CharField(max_length=64)           # SHA-256 of manifest bytes
    cached_bundle_path = CharField(max_length=500, blank=True)  # set by export
    retention_class = CharField(max_length=32)
    availability = CharField(max_length=16, choices=...)        # GENERATED / PURGED / FAILED
    generation_error = CharField(max_length=...)       # populated when FAILED
```

Migration `0044_add_run_evidence_artifact` adds the table without backfill — existing completed runs stay manifest-less until they get re-finalised.

## Generation: `EvidenceManifestBuilder`

Service module at `validibot/validations/services/evidence.py`. Static service:

- `build(validation_run)` → schema-validated Pydantic model
- `serialise(model)` → canonical JSON bytes (`sort_keys=True`, `separators=(",",":")`, `ensure_ascii=True`)
- `persist(model, run)` → writes to storage, hashes the bytes, creates the `RunEvidenceArtifact` row

### Best-effort generation

`stamp_evidence_manifest(run)` wraps the builder and catches every exception. On failure it records `availability=FAILED` with the exception message in `generation_error` and **never re-raises**. Both run-completion paths call the wrapper:

- `step_orchestrator.execute_workflow_steps` (sync runs)
- `validation_callback._finalise_run_for_status` (async runs)

A manifest-stamping failure does **not** fail the run. The validation outcome is preserved; the auditor (`audit_workflow_versions`) surfaces the gap.

## Retention policy — what goes in what tier

Centralised in `validibot/validations/services/evidence_retention.py`. The `RetentionPolicy` class exposes pure decision functions:

- `includes_input_hash(retention_class)` — currently true for every tier;
- `includes_output_hash(retention_class)` — true for `STORE_*` tiers, false under `DO_NOT_STORE`;
- `redactions_for(retention_class)` — returns the list of field names the policy stripped.

Allowlist semantics: anything not explicitly permitted for a retention tier is dropped. Future schema fields fail closed (omitted under `DO_NOT_STORE`) rather than failing open.

### `DO_NOT_STORE` privacy boundary

The manifest for a `DO_NOT_STORE` run includes:

**Always:**

- run id, status, trusted source, timestamps;
- workflow id, slug, version, definition digest;
- validator slug, version, validator class, backend slug, backend version, execution backend, runner, image digest, envelope schema versions;
- logical file type, byte size, content hash (input SHA-256), `content_available=false`;
- pass/fail status, finding counts, stable finding codes, assertion ids where those values do not contain user content;
- credential id, issuer, key id, credential hash, verification URL.

**Excluded under `DO_NOT_STORE`:**

- raw input bytes;
- decoded input text;
- original filenames *unless* the workflow explicitly treats filenames as non-sensitive;
- generated artifacts;
- signed URLs;
- host storage paths;
- environment variables;
- stack traces;
- backend stdout/stderr;
- finding excerpts/messages unless the producing validator marks them safe for evidence.

For `STORE_INPUT` runs, the input bytes are retained alongside the manifest. For `STORE_OUTPUT` runs, the output envelope and allowed artifacts are included. Signed credentials always reference hashes, never raw private data.

### Why the input hash is always present

SHA-256 is preimage-resistant. Recipients of the manifest cannot reconstruct the original bytes from the hash, so retaining the hash doesn't violate the privacy promise. It IS the proof "this run consumed *this exact input*" that makes the manifest meaningful.

The `Submission.checksum_sha256` field is computed at upload time and explicitly preserved through `Submission.purge_content()`. The manifest builder pulls from `Submission.checksum_sha256`, not from a live re-hash of bytes that may already be purged.

### Output hash gating

The output envelope hash (`output_envelope_sha256`) comes from `ValidationRun.output_hash`, populated by `safe_stamp_output_hash` immediately before the manifest stamp. Under `DO_NOT_STORE`, this field is omitted from the manifest and the omission is recorded in `retention.redactions_applied`.

### Logs in the manifest (deferred)

Curated runtime log subsets (step start/end events, finding emit events) require new optional fields in the v1 schema, which means another `validibot-shared` release. Treating it as a follow-up keeps Session B shippable today; the current schema already meets the privacy-leak acceptance criteria because no v1 field exposes payload bytes.

## Manifest hash stability

The manifest hash must be stable enough for signed credentials and support bundles. Canonicalisation rules:

- `sort_keys=True` so ordering changes don't break determinism;
- `separators=(",",":")` so whitespace doesn't shift bytes;
- `ensure_ascii=True` so encoding choice is fixed.

The manifest's identity, contract, and validator metadata is unchanged across retention classes — the proof "this run happened against workflow Y with validator V (digest D) consuming input matching hash H" survives every tier.

If a later export omits purged raw content, it creates a new export artifact but does **not** rewrite the original manifest hash.

## Signed credential ↔ manifest link

When `validibot-pro` issues a signed credential for a run, the credential payload includes the `manifest_hash`. Verifying the credential therefore proves "the credential refers to manifest H" — and re-fetching manifest H + recomputing its hash proves the manifest hasn't been tampered with after signing.

The wiring lives in `validibot-pro/src/validibot_pro/credentials/manifest_link.py`. Verification is in `validibot-pro/src/validibot_pro/credentials/verify.py`:

1. Operator drops a bundle file into a verify form.
2. Server reads `manifest.json` + `manifest.sig`.
3. Verifies the signature against the configured signing key.
4. Recomputes the manifest hash and confirms it matches the credential's claim.
5. Renders a verification result page.

A tampered bundle (manifest bytes altered, or wrong signature) fails verification with an actionable error.

## Audit findings

`audit_workflow_versions` adds two finding codes for evidence:

- `MANIFEST_MISSING` — terminal-state run without an artifact. **Severity warn.** Either the run finished before the stamper deployed, or stamping silently failed before the FAILED row could be recorded.
- `MANIFEST_GENERATION_FAILED` — run with `availability=FAILED`. **Severity error.** The `generation_error` column records why.

```bash
python manage.py audit_workflow_versions --json
```

`--strict` makes warn-level findings exit non-zero. Suitable for CI.

### Remediation

| Finding | Remediation |
|---|---|
| `MANIFEST_MISSING` | Re-finalise the run via the admin or a management script — that triggers the manifest stamper and the row appears. For very old runs (years) where the original workflow has been mutated since, accept legacy versioning and document. |
| `MANIFEST_GENERATION_FAILED` | Read `RunEvidenceArtifact.generation_error` on the row. Common causes: storage backend unreachable, schema validation failure (rare bug). Fix the underlying issue and re-stamp via `EvidenceManifestBuilder.persist(run, EvidenceManifestBuilder.build(run))`. |

## Phase status

| Session | Status (2026-05-02) | Scope |
|---|---|---|
| **Session A** — Schema + manifest generation core | ✓ Complete | `validibot.evidence.v1` in shared 0.5.0+, `RunEvidenceArtifact` model, `EvidenceManifestBuilder`, `stamp_evidence_manifest` best-effort wrapper, both run-completion paths wired, auditor finding codes added |
| **Session B** — Retention enforcement + payload digests | ✓ Complete | `RetentionPolicy`, builder respect for retention, DO_NOT_STORE privacy boundary, manifest hash determinism with digests populated |
| **Session B/2** — Curated logs follow-up | Deferred | New optional v1 fields for log categories; needs another `validibot-shared` release |
| **Session C** — Export UX + signed credential link | Queued | Run-detail "Export evidence bundle" action, verify flow, credential ↔ manifest link |

## Backup and restore implications

The `RunEvidenceArtifact` rows + the evidence storage directory are part of the backup/restore contract. The backup manifest's `included` array lists `run-evidence` as a first-class data category. Restore guarantees that the evidence-index rows and on-disk artefacts are consistent — a row pointing at a missing file is a corruption that doctor flags.

## See also

- [Trust Architecture](trust-architecture.md) — the four invariants
- [Workflow Versioning](../data-model/workflow-versioning.md) — how the trust columns evidence cites are populated and audited
- [Validator Architecture](validator_architecture.md) — the input/output envelopes evidence references
- [Terminology](terminology.md) — the validator vs validator backend distinction the manifest records

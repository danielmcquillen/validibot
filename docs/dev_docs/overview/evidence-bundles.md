# Evidence Bundles

Every completed validation run can produce an **evidence bundle**: a portable artifact that documents what was checked, against what workflow contract, with what validator metadata, with what retention policy, and with what cryptographic payload digests.

This page is the current developer-facing reference for the delivered bundle format. The implementation lives in:

- `validibot/validations/services/evidence.py` — builds and persists `manifest.json`
- `validibot/validations/services/evidence_bundle.py` — builds the downloadable bundle tarball
- `validibot/validations/views/evidence.py` — serves the manifest and bundle download endpoints
- `validibot-shared/validibot_shared/evidence/manifest.py` — shared Pydantic schema used by producers and verifiers

## Submitted, executed, and produced bytes

Evidence must describe what actually crossed the execution boundary, not just
the storage location from which a backend was asked to read. A URI can remain
the same while its content changes, so durable evidence is based on content
identities and immutable execution context.

Preprocessing means one run can legitimately involve several related byte
identities. For an EnergyPlus template run, the useful mental model is:

```text
submitted template bytes A
    → trusted parameter substitution
executed model bytes B
    → validator backend image C
verified output bytes D
    → evidence binds A, B, C, and D to one execution attempt
```

It would be misleading to record only A and imply that those bytes were sent
directly to EnergyPlus. Mature execution evidence records the original and
executed digests separately, describes the transformation between them, and
binds the output and backend image to the same attempt.

The v1 manifest keeps the submitted-content digest in
`payload_digests.input_sha256` and also projects strict runtime evidence under
`execution_attempts`. Each attempt record binds the canonical input envelope,
provider execution, backend image, and retention-permitted output envelope to
the exact input/resource file sizes, SHA-256 digests, and storage versions that
the backend was required to verify while streaming.

`execution_attempts[].input_relationships` makes preprocessing honest. A
direct input is recorded as `identical`; generated EnergyPlus input records
both the original parameter submission and workflow template as
`transformed` sources of the executed model. The projection is deliberately
URI-free and never stores callback nonces or other live capabilities.

`inputs_verified` is true only after trusted completion supplied a verified
output envelope. Failed or interrupted attempts may still appear so auditors
can see what was committed for a retry, but their declared files are not
misrepresented as successfully verified.

## Current Bundle Format

The bundle download is a deterministic `.tar.gz` built on demand. It is not currently cached to storage.

```text
evidence-<run-id>.tar.gz
├── manifest.json
├── README.txt
└── manifest.sig        # only when validibot-pro is installed and the run has a signed credential
```

### `manifest.json`

The canonical JSON evidence manifest for the run. These are the exact bytes stored by `RunEvidenceArtifact.manifest_path`, copied into the bundle without modification.

The manifest is the structured proof of:

- which run completed;
- which workflow version and contract were used;
- which validator steps and validator digests were used;
- which strict execution attempts, providers, backend images, and verified
  input files were used;
- how original or workflow-resource bytes relate to transformed execution
  inputs;
- which input schema applied;
- which retention policy applied;
- which SHA-256 payload digests identify the submitted run input and, where retention permits, output.

### `README.txt`

A human-readable orientation file generated into the tarball. It explains:

- the run ID and workflow version;
- the manifest schema version;
- the manifest SHA-256;
- how to verify the manifest hash;
- whether `manifest.sig` is present;
- that raw input and output bytes are not currently included.

### `manifest.sig`

Included only when:

- `validibot-pro` is installed; and
- the run has an `IssuedCredential`.

The file is the compact-JWS signed credential bytes from `IssuedCredential.credential_jws`. It is byte-for-byte identical to the standalone `credential.jwt` download on the run detail page, but named `manifest.sig` inside the bundle because it acts as a sidecar attestation for `manifest.json`.

The credential contains a `credentialSubject.validationRun.manifestHash` claim. Verifiers recompute `SHA-256(manifest.json)` and compare it with that claim.

## What Is Not In The Current Bundle

The current bundle intentionally does **not** include:

- raw input bytes;
- raw output bytes;
- decoded submission content;
- original execution workspace files;
- `workflow.json`;
- `input/` or `output/` directories;
- runtime logs;
- signed URLs;
- host storage paths;
- environment variables;
- stack traces.

Raw input/output bundle members and curated logs are future work. For now, payload identity is represented by `manifest.json::payload_digests`.

## Download Endpoints

The manifest and bundle are served from validation-run detail routes:

```text
GET /validations/<run-uuid>/evidence/manifest/
GET /validations/<run-uuid>/evidence/bundle/
```

Both endpoints use `ValidationRunAccessMixin`: if a user can see the run detail page, they can download the evidence artifact.

### Manifest Response

The manifest endpoint streams `manifest.json` from storage.

Important response details:

- `Content-Type: application/json`
- `Content-Disposition: attachment; filename="manifest.json"`
- `Cache-Control: no-store, max-age=0`
- `X-Validibot-Manifest-Sha256: <artifact.manifest_hash>`
- `X-Validibot-Schema-Version: <artifact.schema_version>`

It returns `404` when the run has no generated manifest, when manifest generation failed, when the manifest was purged, or when the artifact row has no stored manifest bytes.

### Bundle Response

The bundle endpoint builds the tarball in memory from the generated artifact.

Important response details:

- `Content-Type: application/gzip`
- `Content-Disposition: attachment; filename="evidence-<run-id>.tar.gz"`
- `Cache-Control: no-store, max-age=0`
- `X-Validibot-Manifest-Sha256: <artifact.manifest_hash>`
- `X-Validibot-Schema-Version: <artifact.schema_version>`

It returns `404` when the run has no `RunEvidenceArtifact` in `GENERATED` state or when the stored manifest bytes are unavailable.

## Manifest Schema

The schema version is `validibot.evidence.v1`. The schema lives in `validibot-shared` so external verifiers can parse manifests without importing the Django application.

Public schema symbols:

- `EvidenceManifest` — top-level model
- `WorkflowContractSnapshot` — workflow launch-contract fields at run time
- `StepValidatorRecord` — per-step validator identity and digests
- `ManifestRetentionInfo` — retention class and redactions applied
- `ManifestPayloadDigests` — input/output SHA-256 digests
- `ManifestExecutionInput` — URI-free identity of one strict runtime input
- `ManifestInputRelationship` — original-to-executed byte relationship
- `ManifestExecutionAttempt` — attempt, provider, envelope, image, and input evidence
- `SCHEMA_VERSION` — `"validibot.evidence.v1"`

The current manifest shape is top-level, not nested under `run`, `workflow`, or `submission` objects.

```json
{
  "schema_version": "validibot.evidence.v1",
  "run_id": "4e4de41f-7f63-4af5-8e63-5b69e447f5bb",
  "workflow_id": 42,
  "workflow_slug": "energyplus-preflight",
  "workflow_version": "3",
  "org_id": 7,
  "executed_at": "2026-05-20T02:11:30+00:00",
  "status": "SUCCEEDED",
  "source": "API",
  "workflow_contract": {
    "allowed_file_types": ["json"],
    "input_retention": "STORE_30_DAYS",
    "output_retention": "STORE_30_DAYS",
    "agent_billing_mode": "",
    "agent_price_cents": null,
    "agent_max_launches_per_hour": null,
    "agent_public_discovery": false,
    "agent_access_enabled": false
  },
  "steps": [
    {
      "step_id": 1001,
      "step_order": 1,
      "validator_slug": "json-schema",
      "validator_version": "3",
      "validator_semantic_digest": "sha256:...",
      "validator_backend_image_digest": null
    }
  ],
  "execution_attempts": [
    {
      "execution_attempt_id": "2c0c8780-aed8-47d1-bd7d-a9b83589d712",
      "step_run_id": "29ab683a-8506-4a17-952a-27d88ab906ec",
      "attempt_number": 1,
      "state": "COMPLETED",
      "runner_type": "CloudRunServiceExecutionBackend",
      "execution_deployment_id": "7821a108-7328-4fa9-a3e3-d55f52c7e8aa",
      "deployment_kind": "CLOUD_RUN_SERVICE",
      "deployment_revision": "validibot-validator-service-energyplus-v0-15-0-00001-abc",
      "provider_resource_name": "projects/example/locations/australia-southeast1/services/validibot-validator-service-energyplus-v0-15-0",
      "provider_execution_id": "projects/example/locations/australia-southeast1/queues/validibot-validator-provider/tasks/vb-attempt-2c0c8780aed847d1bd7da9b83589d712",
      "attempt_contract_version": "validibot.attempt.v2",
      "input_envelope_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "output_envelope_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "backend_image_digest": "ghcr.io/example/backend@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      "inputs_verified": true,
      "input_files": [
        {
          "channel": "input_files",
          "name": "model.idf",
          "role": "primary-model",
          "resource_type": "",
          "port_key": "primary_model",
          "resource_id": "",
          "media_type": "application/vnd.energyplus.idf",
          "size_bytes": 1234,
          "sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
          "storage_version": "1749836212345678"
        }
      ],
      "input_relationships": []
    }
  ],
  "input_schema": {
    "type": "object"
  },
  "retention": {
    "retention_class": "STORE_30_DAYS",
    "redactions_applied": []
  },
  "payload_digests": {
    "input_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "output_envelope_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
}
```

> **Manifest field names vs. workflow field names.** The `workflow_contract`
> object keeps the manifest-schema names `agent_public_discovery` and
> `agent_access_enabled` (renaming them is a deferred `validibot-shared`
> release). They map 1:1 to the workflow's current `x402_enabled` and
> `mcp_enabled` fields respectively.

## Payload Digest Naming

The evidence manifest uses schema-specific digest names:

- `payload_digests.input_sha256` — SHA-256 of the submitted data bytes.
- `payload_digests.output_envelope_sha256` — SHA-256 of the canonical output envelope bytes, where retention permits.

The UI may describe the first value as the data hash. Older docs and model comments may use "content hash." In the evidence manifest and bundle, the canonical field name is `payload_digests.input_sha256`.

The input digest is sourced from `Submission.checksum_sha256`, which is computed at upload/submit time and preserved when `Submission.purge_content()` deletes the actual bytes. It identifies the bytes Django accepted. When an advanced validator ran, `execution_attempts[].input_files` separately identifies the strict execution-boundary bytes and `input_relationships` explains whether they were identical or transformed.

The output digest is sourced from `ValidationRun.output_hash`, populated before the manifest is stamped.

## Retention Policy

Evidence retention rules are centralized in `validibot/validations/services/evidence_retention.py`.

Current rules:

- `payload_digests.input_sha256` is included for every retention tier, including `DO_NOT_STORE`.
- `payload_digests.output_envelope_sha256` is omitted for `DO_NOT_STORE`.
- omitted fields are listed in `retention.redactions_applied`.

For `DO_NOT_STORE`, the manifest still preserves the digest of the exact bytes Django accepted and the URI-free strict input identities. The bundle does not include those bytes and the UI/API must not expose them even if the async reaper has not deleted them yet. Attempt output-envelope digests are redacted under the same output-hash rule, while `inputs_verified` still records whether trusted completion crossed the strict input-verification boundary.

## Run Source Attribution

The optional `source` field documents which authenticated route produced the run.

Allowed values:

| Value | Route |
| --- | --- |
| `LAUNCH_PAGE` | Browser workflow launch form |
| `API` | REST API workflow run endpoint |
| `MCP` | Authenticated MCP helper API |
| `X402_AGENT` | Cloud-only x402 anonymous-agent route |
| `CLI` | `validibot` command-line tool |
| `SCHEDULE` | Scheduled or automated run |

`source` must be derived by the authenticated route, never from a caller-controlled request header.

## Storage And Database Index

Manifest bytes live in configured application storage, not as database blobs.

Current storage path:

```text
evidence/<org-id>/<run-id>/manifest.json
```

The database index is `RunEvidenceArtifact`, a one-to-one row with `ValidationRun`.

Important fields:

- `schema_version` — manifest schema string, usually `validibot.evidence.v1`
- `manifest_path` — `FileField` pointing to the stored `manifest.json`
- `manifest_hash` — SHA-256 of the canonical JSON bytes
- `retention_class` — workflow input retention class used by the manifest builder
- `availability` — `GENERATED`, `PURGED`, or `FAILED`
- `generation_error` — populated when availability is `FAILED`
- `cached_bundle_path` — reserved for future cached bundle exports; current downloads are built on demand and do not populate it

Migration `0044_add_run_evidence_artifact` adds this model without backfill. Older completed runs may be manifest-less until re-finalized or explicitly re-stamped.

## Generation

`EvidenceManifestBuilder` in `validibot/validations/services/evidence.py` is the manifest producer.

- `build(run)` returns a schema-validated `EvidenceManifest`.
- `serialise(manifest)` returns canonical JSON bytes.
- `persist(run, manifest)` writes the manifest to storage, computes `manifest_hash`, and updates the `RunEvidenceArtifact` row.

`stamp_evidence_manifest(run)` wraps build + persist and catches every exception. Manifest generation is best effort: a failure records `availability=FAILED` and does not change the validation run result.

Both run-completion paths call the wrapper:

- synchronous step orchestration;
- asynchronous validation callback finalization.

## Determinism And Verification

The manifest hash is computed over canonical JSON:

- `sort_keys=True`
- `separators=(",", ":")`
- `ensure_ascii=True`

The bundle tarball is also deterministic:

- tar member names are stable;
- tar member mode, mtime, uid, gid, uname, and gname are normalized;
- gzip `mtime` is fixed to `0`.

Verification flow for signed bundles:

1. Extract `manifest.json` and `manifest.sig`.
2. Recompute `SHA-256(manifest.json)`.
3. Parse `manifest.sig` as a compact-JWS verifiable credential.
4. Verify the JWS against the issuer JWKS.
5. Compare the recomputed manifest hash with `credentialSubject.validationRun.manifestHash`.
6. Reject the bundle if the hashes differ.

Community deployments without `validibot-pro` still produce `manifest.json` and `README.txt`, but they do not produce `manifest.sig`.

## Deferred Bundle Members

The original evidence architecture described a future richer bundle with members such as:

```text
workflow.json
input/
results/
logs/
credentials/
```

Those members are not part of the current delivered bundle. Adding them requires a focused retention review because raw inputs, outputs, logs, and file names may carry user content. Until then, the manifest's payload digests, execution-attempt records, and artifact lineage are the portable evidence of byte identity.

## Audit Findings

`audit_workflow_versions` emits evidence-manifest findings:

- `MANIFEST_MISSING` — terminal run has no artifact row.
- `MANIFEST_GENERATION_FAILED` — artifact exists but `availability=FAILED`.

```bash
python manage.py audit_workflow_versions --json
```

`--strict` makes warning-level findings return a non-zero exit status.

## See Also

- [Trust Architecture](trust-architecture.md)
- [Workflow Versioning](../data-model/workflow-versioning.md)
- [Validator Architecture](validator_architecture.md)
- [Terminology](terminology.md)

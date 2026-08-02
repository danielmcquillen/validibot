# Evidence Bundles

An evidence bundle is a portable receipt for a completed validation run. It
proves the run outcome, the workflow steps and validator versions involved, and
the canonical identities of the submitted input and output envelope. It does
not contain input or output bytes.

The implementation lives in:

- `validibot/validations/services/evidence.py` — builds and persists the receipt;
- `validibot/validations/services/evidence_bundle.py` — builds the tarball;
- `validibot/validations/views/evidence.py` — serves manifest and bundle routes;
- `validibot-shared/validibot_shared/evidence/manifest.py` — shared v2 model.

## Current bundle format

Bundles are deterministic `.tar.gz` files built on demand:

```text
evidence-<run-id>.tar.gz
├── manifest.json
├── README.txt
└── credential.jwt      # only when Pro issued a credential
```

`manifest.json` is copied byte-for-byte from the stored evidence artifact.
`README.txt` provides orientation. `credential.jwt` is the complete compact-JWS
credential and uses the same filename as the standalone credential download.
It contains the `manifestHash` claim that binds the credential to
`manifest.json`.

## Permanent manifest

The manifest is a small, permanently retained audit receipt. Its exact top
level is:

```json
{
  "$schema": "https://validibot.com/schemas/evidence-manifest-v2.json",
  "run_id": "4e4de41f-7f63-4af5-8e63-5b69e447f5bb",
  "completed_at": "2026-08-02T04:12:55+00:00",
  "status": "SUCCEEDED",
  "workflow": {
    "slug": "energyplus-preflight",
    "version": "3",
    "steps": [
      {
        "key": "validate_input",
        "status": "SUCCEEDED",
        "validator": "json-schema",
        "validator_version": "3",
        "backend_image_digest": "registry.example/json-schema@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    ]
  },
  "input_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "output_envelope_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
}
```

The workflow projection contains the ordered validator steps and their
statuses. `backend_image_digest` is optional because capture is best effort;
when present, it distinguishes rebuilt backend images that share a validator
version. This is the only execution-environment identity retained in the
receipt.

The receipt deliberately does not contain:

- raw input or output bytes;
- `allowed_file_types`, input schemas, retention settings, billing/access
  configuration, or other workflow policy;
- filenames, labels, descriptions, or other free-form author metadata;
- provider resource names, provider execution IDs, service-account identities,
  or operational telemetry;
- workflow-definition hashes, lineage graphs, per-file digests, executed-input
  digests, preprocessing relationships, sizes, or storage versions.

The receipt attests the canonical submission and declared workflow. For a
generate-then-execute workflow, the bytes submitted and the bytes that cross
the execution boundary can differ; the current minimal receipt accepts that
distinction.

## Retention boundary

Input and output retention controls payload bytes only. It does not select
manifest fields and it does not delete the permanent receipt. The top-level
`input_sha256` and `output_envelope_sha256` values remain in the receipt after
the corresponding bytes are purged. An explicit account or tenant erasure
requirement may still remove the receipt.

There is no separate evidence-retention policy, workflow setting, redaction
pass, or evidence-specific purge command. The output sweep must leave the
stored manifest intact; `PURGED` is reserved for explicit evidence erasure or
legacy rows whose receipt was already deleted.

## Download endpoints

```text
GET /validations/<run-uuid>/evidence/manifest/
GET /validations/<run-uuid>/evidence/bundle/
```

Both endpoints use `ValidationRunAccessMixin`. The manifest endpoint returns
`manifest.json`; the bundle endpoint builds the deterministic tarball. Both
include the stored manifest SHA-256 and schema URL in response headers.

The manifest remains available after payload purge. A run without a generated
manifest, a failed generation, or an explicitly erased receipt returns the
corresponding unavailable response.

## Verification

For a signed bundle:

1. Extract `manifest.json` and `credential.jwt`.
2. Recompute `SHA-256(manifest.json)`.
3. Parse and verify the JWS in `credential.jwt` against the issuer JWKS.
4. Compare the recomputed digest with
   `credentialSubject.validationRun.manifestHash`.
5. Reject the bundle if the digests differ.

Community deployments still produce `manifest.json` and `README.txt`, but do
not produce `credential.jwt` and therefore cannot provide signed verification.

## Generation and storage

`EvidenceManifestBuilder` performs three operations:

- `build(run)` returns a schema-validated `EvidenceManifest`;
- `serialise(manifest)` produces canonical JSON bytes using sorted keys,
  compact separators, and ASCII escaping;
- `persist(run, manifest)` stores those exact bytes and hashes them for the
  `RunEvidenceArtifact` index row.

`stamp_evidence_manifest(run)` is best effort. A generation failure records
`availability=FAILED` without changing the validation outcome. Both synchronous
orchestration and asynchronous callback finalization stamp the receipt before
emitting `validation_run_finalized`, because that signal schedules retention.

Manifest bytes live in configured application storage at a path such as:

```text
evidence/<org-id>/<run-id>/manifest.json
```

The database index stores the schema URL, manifest hash, storage path, and
availability. Legacy retention metadata may remain on the index row for
compatibility, but it is not part of the manifest and does not control its
shape.

## Related decisions

- [Evidence Bundle and Signed Credential — Binding, Format, and Access](../../../validibot-project/docs/adr/2026-07-31-evidence-bundle-and-credential-binding.md)
- [Evidence Manifest — Permanent, Minimal Audit Receipt](../../../validibot-project/docs/adr/2026-07-31-evidence-retention.md)
- [Publishing the Evidence and Credential Schemas](../../../validibot-project/docs/adr/2026-07-31-evidence-schema-publication.md)

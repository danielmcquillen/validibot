# Evidence Bundles

An evidence bundle is the smallest portable record of a completed validation
run. It contains no submitted content or generated output files.

The proof has one relationship:

```text
credential.jwt -- manifestHash --> manifest.json
```

The credential's signature authenticates its `manifestHash` claim. Re-hashing
`manifest.json` proves whether the supplied receipt is the exact receipt named
by that credential.

## Bundle contents

The downloaded archive contains exactly:

```text
evidence-<run-id>.tar.gz
├── manifest.json
└── credential.jwt      # only when Pro issued a credential
```

There is no README, descriptor, raw payload directory, or separate signature
file. The fixed filenames are the format. `credential.jwt` is the same compact
JWS available from the standalone credential download.

A Community archive contains only `manifest.json`. It is an audit record but
not cryptographic proof. A Pro archive becomes verifiable when it also contains
`credential.jwt`.

## Permanent manifest

The v2 manifest has this shape:

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
        "status": "PASSED",
        "validator": "energyplus",
        "validator_version": "3",
        "backend_image_digest": "registry.example/energyplus@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    ]
  },
  "input_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "output_envelope_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
}
```

`backend_image_digest` is optional because not every validator execution has a
container image. When present, it distinguishes different executable builds
that share the same validator version.

The receipt deliberately excludes raw payloads, filenames, storage locations,
retention settings, accepted-file policy, embedded input schemas, full workflow
configuration, lineage, provider execution identifiers, executed-input
snapshots, and per-artifact graphs.

The execution attempt still captures detailed input evidence before provider
execution. Input retention may later redact that operational snapshot. It is
not projected into the permanent portable receipt.

## Finalization and binding

For a successful credential-bearing run, finalization is ordered:

1. build the run summary;
2. stamp the output digest;
3. build, canonicalize, store, and hash `manifest.json`;
4. issue `credential.jwt` with the stored manifest hash; and
5. emit `validation_run_finalized`, which schedules payload retention.

Credential issuance refuses a missing or failed manifest. There is no legacy
mode that issues an unbound credential. If a blocking issuance failure changes
the run result, the summary, output digest, and manifest are rebuilt before the
finalized signal is sent.

## Retention boundary

Input and output retention controls payload bytes. The evidence manifest is a
permanent audit receipt for the life of its owning run or tenant record, subject
to a future explicit erasure policy.

Output purge removes detailed findings, artifacts, envelopes, step values, and
other payload-derived output state. It leaves `manifest.json` and the evidence
index row available. The receipt keeps the input/output digests as records about
the deleted payloads; documentation must not describe those hashes as anonymous.

There is no evidence-retention dropdown, evidence deadline, evidence purge
sweep, retention-specific manifest shape, or `PURGED` manifest state.

## Download and verification

The existing authenticated run routes are:

```text
GET /validations/<run-uuid>/evidence/manifest/
GET /validations/<run-uuid>/evidence/bundle/
```

They use the same organization-scoped access rules as the run detail page and
remain available after ordinary payload expiry.

To verify a signed bundle:

1. extract `manifest.json` and `credential.jwt`;
2. verify the compact JWS with the issuing instance's registered public key;
3. compute SHA-256 over the exact `manifest.json` bytes; and
4. compare it with `credentialSubject.validationRun.manifestHash`.

Both checks must pass. Validibot verifies only credentials issued by the local
instance and does not fetch arbitrary remote JWKS documents.

## Code map

- `validibot/validations/services/evidence.py` builds and stores the receipt.
- `validibot/validations/services/evidence_bundle.py` creates the minimal archive.
- `validibot/validations/views/evidence.py` serves authenticated downloads.
- `validibot/validations/services/retention.py` purges payloads while preserving
  the receipt.
- `validibot-shared/validibot_shared/evidence/manifest.py` defines the v2 model.
- `validibot-pro/credentials/issuance.py` enforces the binding prerequisite.
- `validibot-pro/credentials/verify.py` verifies the signature and binding.

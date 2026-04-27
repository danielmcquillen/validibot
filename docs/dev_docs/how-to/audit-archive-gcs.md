# Audit log archive on GCS

Point the `enforce_audit_retention` management command at a
CMEK-encrypted GCS bucket. Works for self-hosted Pro deployments on
GCP and for Validibot Cloud — both use the same backend class at
[`validibot/audit/backends/gcs.py`](../../../validibot/audit/backends/gcs.py).

Two moving parts:

1. **`enforce_audit_retention`** — the scheduled command that picks
   rows past `AUDIT_HOT_RETENTION_DAYS`, hands them to the
   configured archive backend, and deletes only the ids the backend
   confirmed. Lives at
   [`validibot/audit/management/commands/enforce_audit_retention.py`](../../../validibot/audit/management/commands/enforce_audit_retention.py).
2. **`GCSArchiveBackend`** — writes JSONL+gzip partitions to a
   CMEK-encrypted GCS bucket and verifies each write by re-reading
   the object and comparing SHA-256.

The schedule (daily at 02:30) comes from the task registry entry;
the command and backend are community code on every tier.

## Enabling on a deployment

Set `AUDIT_ARCHIVE_BACKEND` to the GCS backend's dotted path:

```bash
AUDIT_ARCHIVE_BACKEND=validibot.audit.backends.gcs.GCSArchiveBackend
```

The hosted Validibot Cloud settings already default to this. A
self-hosted Pro deployment on GCP sets it via the env var above (in
`.envs/.production/.google-cloud/.django`, Secret Manager, or
whatever env-plumbing you use).

## Settings reference

All read from Django settings; all surfaced as env vars at
[`config/settings/base.py`](../../../config/settings/base.py) so
they flow through the usual env-config path.

| Setting | Required when backend is GCS | Default | Purpose |
|---|---|---|---|
| `AUDIT_ARCHIVE_GCS_BUCKET` | **Yes** | `""` | GCS bucket name (no `gs://` prefix). A Django system check (`validibot.audit.E001`) fails at startup if this is empty while the GCS backend is selected. |
| `AUDIT_ARCHIVE_GCS_PREFIX` | No | `"audit/"` | Object-name prefix. Keeps audit archives out of the way if the bucket is shared. |
| `AUDIT_ARCHIVE_GCS_KMS_KEY` | No | `""` | Fully-qualified CMEK key resource name (`projects/.../cryptoKeys/...`). When empty, objects inherit the bucket's default CMEK. Set this if you want a dedicated per-app key (Google's recommendation for high-sensitivity data). |
| `AUDIT_ARCHIVE_GCS_PROJECT_ID` | No | `""` (ADC) | Override the GCP project id for the storage client. |

Inherited community knobs still apply:

* `AUDIT_HOT_RETENTION_DAYS` — default 90. Rows older are archive candidates.
* `AUDIT_RETENTION_ENABLED` — default `True`. Flip to `False` during an incident to freeze the table; the scheduled task becomes a logged no-op.

## Bucket provisioning

The shape below is what the hosted cloud deploys provision
automatically. Self-hosted Pro operators on GCP should aim for the
same shape — a single `gcloud` / Terraform run puts everything in
place.

1. **Dedicated bucket.** Regional (same region as your Cloud Run
   services), Standard storage class, uniform bucket-level access,
   public-access prevention enforced. A good naming convention is
   `<project>-validibot-audit-archive[-<stage>]` — prefixing with
   the project ID keeps the name unique in GCS's global namespace.
2. **CMEK key.** Keyring + key in the same region as the bucket,
   purpose `ENCRYPT_DECRYPT`, 30-day rotation. Grant the GCS
   service agent `cryptoKeyEncrypterDecrypter` so bucket default
   encryption works, and the Cloud Run service account the same
   role so the backend's explicit `blob.kms_key_name` path also
   works.
3. **Bucket default encryption** points at that key.
4. **Lifecycle rules.** Tier down as data ages:
   * Nearline at 30 days
   * Coldline at 180 days
   * Archive at 365 days

   **Do not** set a delete rule — audit archives are meant to
   outlive Cloud SQL, and the regulatory horizon varies by
   jurisdiction.
5. **IAM on the bucket.** Cloud Run service account gets
   `roles/storage.objectCreator` and `roles/storage.objectViewer`
   (viewer is needed for the verify-on-read step). Deliberately
   does **not** get `objectAdmin` — audit archives are append-only
   from the app's perspective.
6. **Object versioning — off.** The backend produces unique object
   names per retention call; versioning would duplicate every write
   without value.

The hosted Validibot deployment automates the above via a
private `just audit-archive setup <stage>` recipe. Self-hosted
operators running Pro on GCP can replicate the steps with the
`gcloud` commands directly — the list above is the full set.

### Non-GCP self-hosters

If you run Pro outside GCP (AWS, Azure, on-prem), you have three
options:

* **Keep the null backend.** Retention still prunes rows from
  Cloud SQL so the table doesn't grow unbounded; the audit trail
  just isn't archived anywhere. Acceptable for deployments that
  don't have a compliance-driven retention horizon.
* **Use the filesystem backend** with a durable mounted volume.
  `AUDIT_ARCHIVE_BACKEND=validibot.audit.archive.FilesystemArchiveBackend`
  + `AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH=/path/to/archive`.
* **Bring your own backend.** Implement the
  :class:`AuditArchiveBackend` protocol (one method:
  `archive(entries) -> ArchiveReceipt`) and point the setting at
  your dotted path. S3, Azure Blob, and object-store-as-a-service
  backends are all a short implementation away — see
  [`extend-the-audit-log.md`](extend-the-audit-log.md#writing-your-own-backend)
  for the contract and an S3 starter snippet.

## Object layout

Matches the filesystem backend byte-for-byte, so downstream tooling
works the same across backends:

```
gs://<bucket>/<prefix>/org_<id>/YYYY/MM/DD<suffix>.jsonl.gz
gs://<bucket>/<prefix>/org_<id>/YYYY/MM/DD<suffix>.jsonl.gz.sha256
```

`<suffix>` is `T<HHMMSSZ>-<16 hex>` — unique to each archive call so
chunks for the same day never overwrite each other. The `.sha256`
sidecar stores the hex digest of the gzipped body:

```bash
gcloud storage cp gs://.../audit/org_7/2026/04/22T023015Z-<hex>.jsonl.gz.sha256 -
# a4d1...e89f  22T023015Z-<hex>.jsonl.gz
```

Verify any archive years later with:

```bash
gcloud storage cp gs://.../audit/org_7/2026/04/22T023015Z-<hex>.jsonl.gz .
shasum -a 256 22T023015Z-<hex>.jsonl.gz | cut -d' ' -f1
# compare against the .sha256 sidecar
```

## Operations

### Watching a run

The command logs each chunk's outcome to stdout and emits
structured log entries via `logger.info("audit_retention_*")`. In
Cloud Logging:

```
resource.type="cloud_run_revision"
resource.labels.service_name="validibot-worker"
jsonPayload.message=~"audit_retention_"
```

The "completed" line carries `considered / archived / deleted /
actors_deleted` counts. A healthy day shows `archived == deleted`.
Any run where `archived > deleted` means a backend verify failed;
inspect the prior `audit_archive_gcs_verify_failed` warnings in the
same run.

### Stop the music

Freeze the table without stopping the scheduled task:

```
AUDIT_RETENTION_ENABLED=false
# redeploy or restart the worker
```

The scheduled run continues firing at 02:30 every night and logs
`"AUDIT_RETENTION_ENABLED=False — retention is frozen"` each time —
operators reading logs see the command ran; it just did nothing.

### Ad-hoc cleanup

The CLI flags all compose with the GCS backend:

```bash
# Dry-run: count eligible rows, no backend call, no delete.
gcloud run jobs execute validibot-worker-<env> \
  --args="python,manage.py,enforce_audit_retention,--dry-run"

# Tighter window after a cleanup day.
... --args="python,manage.py,enforce_audit_retention,--retention-days,30"
```

## Failure modes worth knowing

| Symptom | Diagnosis | Fix |
|---|---|---|
| `audit_archive_gcs_verify_failed` logs on every run | Object corruption between upload and re-read. | Check bucket settings, confirm CMEK key is accessible to the service account. |
| System check `validibot.audit.E001` at startup | `AUDIT_ARCHIVE_GCS_BUCKET` empty while backend is GCS. | Set the env var and redeploy. |
| `archived == 0` every night despite old rows | The backend is raising on every chunk; the command translates that to `CommandError`. | Check worker logs for `audit_retention_backend_failed` — typically an IAM permission error on the bucket or KMS key. |
| `deleted == 0` but `archived > 0` | DB transaction failed after a successful archive. | Rare; check worker logs for the transaction error. The next scheduled run will re-archive + re-delete. |

## See also

* [Extend the audit log → Retention and archival](extend-the-audit-log.md#retention-and-archival) — developer-facing reference for the protocol contract and CLI flags.
* [validibot-project audit-log design](../../../../validibot-project/docs/observability/audit-log.md) — status matrix + GDPR erasure model.
* [ADR-2026-04-16: Audit log and privacy architecture](../../../../validibot-project/docs/adr/2026-04-16-audit-log-and-privacy-architecture.md).

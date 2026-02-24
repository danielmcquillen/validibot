# Submission Data in Advanced Validators

When an advanced validator (EnergyPlus, FMU) runs, the submission content needs to travel from the database into a container that can't access Django. This document explains how that works, how the data gets cleaned up afterwards, and the tradeoffs behind the current approach.

For container interface details, see [Advanced Validator Interface](validator_architecture.md). For backend infrastructure, see [Execution Backends](execution_backends.md). For retention policies, see [Submissions (Data Model)](../data-model/submissions.md).

## The Execution Bundle

Every advanced validation run creates an **execution bundle** — a directory in storage containing everything the container needs:

```
runs/{org_id}/{run_id}/
├── input.json           # Input envelope (validator config, file URIs, callback info)
├── <submission file>    # Copy of the submission content (see below)
└── output.json          # Written by the container after execution
```

The submission file name and format depend on the validator type. For example, an EnergyPlus run might contain `model.epjson` or `model.idf`, while an FMU run references the FMU model via URI in the envelope rather than copying it into the bundle. Each validator's launcher determines what gets written here.

The bundle path uses the org ID prefix for multi-tenant isolation. Both Docker Compose (`file://`) and GCP (`gs://`) backends follow this same structure.

## Why We Copy the Submission

The execution backend **copies** the submission content into the bundle rather than referencing the original location. There are three reasons for this:

**1. The container can't read from the database.** Submissions are often stored inline in the `Submission.content` database field, not as files on disk. The container has no database access, so the content must be written to storage where the container can read it.

**2. Isolation from concurrent changes.** If the original submission were purged mid-run (possible with `DO_NOT_STORE` retention and unlucky timing), the container would fail trying to read a deleted file. The copy ensures the container always has the data it needs for the duration of the run.

**3. Debuggability.** When investigating a failed run, all inputs and outputs live in one directory. You can inspect exactly what the container received without cross-referencing other storage locations.

The downside is storage duplication, especially on GCP where both the original and copy may live in the same GCS bucket. We've considered referencing the original URI instead (see below), but the complexity isn't worth the marginal cost saving.

## Data Flow

### Docker Compose (Synchronous)

```
1. Celery worker receives validation task
2. DockerComposeExecutionBackend.execute():
   a. Reads submission content from DB
   b. Writes copy to file:///app/storage/runs/{org}/{run}/{filename}
   c. Builds input envelope, writes to runs/{org}/{run}/input.json
   d. Spawns Docker container (blocking)
   e. Container reads input, writes output.json
   f. Worker reads output.json, processes results
3. Step completes → queue_submission_purge() called
```

### GCP Cloud Run (Asynchronous)

```
1. Celery worker receives validation task
2. GCPExecutionBackend.execute() → Cloud Run launcher:
   a. Reads submission content from DB
   b. Uploads copy to gs://bucket/runs/{org}/{run}/model.epjson
   c. Builds input envelope, uploads to gs://bucket/runs/{org}/{run}/input.json
   d. Triggers Cloud Run Job (non-blocking)
   e. Returns immediately with pending status
3. Container runs in Cloud Run, writes output to GCS
4. Container POSTs callback to worker service
5. ValidationCallbackService processes results
6. Run finalized → _queue_purge_if_do_not_store() called
```

## Cleanup and Retention

After a run completes, the execution bundle needs to be cleaned up according to the submission's retention policy. There are two cleanup mechanisms:

### Submission Purging (Primary)

When `Submission.purge_content()` runs (triggered by retention expiration or `DO_NOT_STORE` policy), it:

1. Deletes the original submission content (DB field and/or `input_file`)
2. Calls `_delete_run_files(run)` for **every related validation run**
3. `_delete_run_files()` calls `storage.delete_prefix(f"runs/{org_id}/{run_id}/")` which removes the entire execution bundle

This is storage-agnostic — it works for both local filesystem and GCS.

### Output Expiration (Secondary)

The `purge_expired_outputs` management command independently cleans up execution bundles when the output retention period expires. This acts as a safety net if submission purging fails.

### DO_NOT_STORE Flow

For submissions with `DO_NOT_STORE` retention:

1. Run completes (either sync or via async callback)
2. `queue_submission_purge()` creates a `PurgeRetry` record
3. `process_purge_retries` scheduled job picks it up (runs every 5 minutes)
4. Calls `submission.purge_content()` which deletes both the original content and all execution bundle copies
5. On failure: exponential backoff retries (1m, 5m, 1h, 6h, 24h), max 5 attempts

!!! note "Window of exposure"
    Between run completion and purge execution (typically under 5 minutes), the submission data exists in both the original location and the execution bundle. This is acceptable because the storage is access-controlled, and the window is short.

## Alternative Considered: Reference Original URI

[Issue #73](https://github.com/danielmcquillen/validibot/issues/73) proposed referencing the original submission URI in the input envelope instead of copying. We decided against this because:

- Docker Compose requires the copy regardless (DB content isn't a file)
- Introduces ordering dependencies between purge and container execution
- Marginal storage savings on GCP (same bucket) don't justify the complexity
- Copy approach is simpler to reason about and debug

## Key Code Locations

| Component | File |
|-----------|------|
| Docker Compose backend | `validations/services/execution/docker_compose.py` |
| GCP backend | `validations/services/execution/gcp.py` |
| Cloud Run launcher | `validations/services/cloud_run/launcher.py` |
| Submission purge | `submissions/models.py` → `purge_content()` |
| Run file deletion | `submissions/models.py` → `_delete_run_files()` |
| Purge retry queue | `submissions/models.py` → `queue_submission_purge()` |
| Output expiration | `validations/management/commands/purge_expired_outputs.py` |
| Callback handler | `validations/services/validation_callback.py` |

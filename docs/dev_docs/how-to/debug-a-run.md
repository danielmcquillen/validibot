# How to Debug a Validation Run

When a validation run fails or produces unexpected results, follow this guide to diagnose and resolve the issue.

## Understanding Run Status

Each validation run has a status and optionally an error category:

| Status      | Meaning                                         |
| ----------- | ----------------------------------------------- |
| `PENDING`   | Run created but not yet started                 |
| `RUNNING`   | Validation is in progress                       |
| `SUCCEEDED` | Validation completed successfully               |
| `FAILED`    | Validation found issues or encountered an error |
| `CANCELED`  | Run was manually canceled                       |
| `TIMED_OUT` | Run exceeded the time limit                     |

When a run fails, the `error_category` field indicates why:

| Error Category      | Meaning                              | Common Causes                                                   |
| ------------------- | ------------------------------------ | --------------------------------------------------------------- |
| `VALIDATION_FAILED` | Validator found issues with the file | Invalid file format, schema violations, missing required fields |
| `TIMEOUT`           | Exceeded time limit                  | Large/complex file, slow validator                              |
| `OOM`               | Out of memory                        | File too large for container memory                             |
| `RUNTIME_ERROR`     | Validator crashed                    | Bug in validator, corrupt input file                            |
| `SYSTEM_ERROR`      | Infrastructure issue                 | GCS unavailable, Cloud Run scaling issues                       |

## Step 1: Check the Run Details in UI

1. Navigate to the validation run in the web UI
2. Look at the **Run Summary** card for:
   - Status and error category (if failed)
   - Duration (unusually long may indicate issues)
   - Total findings (errors, warnings, info)
3. Check the **Step Findings** for specific validation messages

## Step 2: Check Cloud Logging

For more detailed diagnostics, use Cloud Logging in the GCP Console or CLI.

### Find logs for a specific run

```bash
# Replace RUN_ID with the actual run UUID
gcloud logging read "jsonPayload.message:\"run_id=RUN_ID\" OR jsonPayload.run_id=\"RUN_ID\"" \
  --limit=50 \
  --format="table(timestamp,jsonPayload.severity,jsonPayload.message)"
```

### Find recent errors

```bash
# Errors from the worker service in the last hour
gcloud logging read "resource.labels.service_name=\"$GCP_APP_NAME-worker\" severity>=ERROR timestamp>=\"-1h\"" \
  --limit=20
```

### Check Cloud Run Job logs (for EnergyPlus/FMU validators)

```bash
# EnergyPlus validator job logs
gcloud logging read "resource.type=\"cloud_run_job\" resource.labels.job_name=\"$GCP_APP_NAME-validator-energyplus\"" \
  --limit=50

# FMU validator job logs
gcloud logging read "resource.type=\"cloud_run_job\" resource.labels.job_name=\"$GCP_APP_NAME-validator-fmu\"" \
  --limit=50
```

## Step 3: Common Issues and Solutions

### TIMEOUT

**Symptoms**: Run status is `TIMED_OUT` or `error_category` is `TIMEOUT`

**Causes**:

- File is too large or complex
- Validator is processing too slowly

**Solutions**:

1. Try with a smaller/simpler test file
2. Check if the file has unusual complexity (deep nesting, many objects)
3. Contact support if files within normal size range are timing out

### OOM (Out of Memory)

**Symptoms**: `error_category` is `OOM`, Cloud Run Job terminated

**Causes**:

- File requires more memory than container limit
- Memory leak in validator

**Solutions**:

1. Check file size - very large files may exceed memory limits
2. Try a smaller file to confirm the validator works
3. Contact support to request higher memory limits

### RUNTIME_ERROR

**Symptoms**: `error_category` is `RUNTIME_ERROR`, unexpected exception

**Causes**:

- Corrupt or malformed input file
- Edge case in validator code
- Missing dependencies

**Solutions**:

1. Validate the input file is not corrupt
2. Try the same file type with a known-good example
3. Check Cloud Logging for stack traces
4. Report the issue with the run ID if it's a validator bug

### VALIDATION_FAILED

**Symptoms**: `error_category` is `VALIDATION_FAILED`

This is the expected outcome when the validator finds issues with your file. Check the findings for details about what failed validation.

### Callback Not Received

**Symptoms**: Run stuck in `RUNNING` status indefinitely

**Causes**:

- Cloud Run Job failed to send callback
- Network/IAM issues between job and worker
- Callback endpoint rejected the request

**Diagnosis**:

1. Check Cloud Run Job execution status:
   ```bash
   gcloud run jobs executions list --job=$GCP_APP_NAME-validator-energyplus
   ```
2. Check worker logs for callback attempts:
   ```bash
   gcloud logging read "resource.labels.service_name=\"$GCP_APP_NAME-worker\" jsonPayload.message:\"callback\"" --limit=20
   ```

## Step 4: Django Admin

For operators with admin access:

1. Go to `/admin/validations/validationrun/`
2. Find the run by ID or filter by status
3. Examine:
   - `status`, `error_category`, `error` fields
   - `summary` JSON field for full envelope data
   - Related `ValidationStepRun` entries
   - Related `ValidationFinding` entries

## Step 5: Database Queries

For direct database access:

```sql
-- Find a specific run
SELECT id, status, error_category, error, started_at, ended_at, duration_ms
FROM validations_validationrun
WHERE id = 'your-run-uuid';

-- Find recent failed runs
SELECT id, status, error_category, created
FROM validations_validationrun
WHERE status = 'FAILED'
ORDER BY created DESC
LIMIT 20;

-- Find runs with specific error category
SELECT id, status, error_category, error, created
FROM validations_validationrun
WHERE error_category = 'TIMEOUT'
ORDER BY created DESC
LIMIT 20;
```

## Getting Help

If you can't resolve the issue:

1. Note the **run ID** (UUID shown in the URL or run details)
2. Note the **timestamp** when the issue occurred
3. Note the **file type** and approximate file size
4. Contact support with these details

## Related

- [Cloud Logging](../google_cloud/logging.md) - Detailed logging guide
- [Deployment Guide](../google_cloud/deployment.md) - Infrastructure overview

# How to Run Integration Tests

This guide covers running integration and end-to-end tests for Validibot.

## Test Categories

Validibot has several types of tests:

| Type | Location | What It Tests |
|------|----------|---------------|
| Unit tests | `validibot/*/tests/` | Individual functions and classes |
| Integration tests | `tests/tests_integration/` | Cloud Run Jobs, GCS, database |
| E2E tests | `tests/tests_integration/test_e2e_workflow.py` | Full API -> callback flow |
| Use case tests | `tests/tests_use_cases/` | Business scenarios |

## Quick Reference

```bash
# Run all unit tests
uv run --extra dev pytest validibot/

# Run integration tests (starts local Postgres)
just test-integration

# Run E2E tests against deployed staging
E2E_TEST_API_URL=https://your-app.run.app/api/v1 \
E2E_TEST_API_TOKEN=your-token \
E2E_TEST_WORKFLOW_ID=workflow-uuid \
just test-e2e
```

## Integration Tests

Integration tests verify Cloud Run Job execution and GCS operations. They run against real GCP infrastructure but skip callbacks (since there's no Django endpoint listening).

### Prerequisites

- GCP credentials configured (`gcloud auth application-default login`)
- Required environment variables (see below)
- Docker Compose available

### Running Integration Tests

```bash
# Run all integration tests
just test-integration

# Run a specific test file
just test-integration tests/tests_integration/test_validator_jobs.py

# Run with extra verbosity
just test-integration -vvv
```

### Required Environment Variables

For Cloud Run Job tests:

```bash
export GCP_PROJECT_ID=your-project
export GCS_VALIDATION_BUCKET=your-bucket
export GCS_ENERGYPLUS_JOB_NAME=validibot-validator-energyplus
export GCS_FMI_JOB_NAME=validibot-validator-fmi
export GCP_REGION=australia-southeast1
```

These are typically set in `_envs/local/set-env.sh` or loaded from `.envs/`.

## E2E Tests

E2E tests verify the complete validation flow including callbacks. Unlike integration tests, these require a deployed staging environment where Cloud Run Jobs can call back to Django.

### What E2E Tests Cover

1. Submit file via API
2. Django creates ValidationRun
3. Django launches a Cloud Run Job (Jobs API)
4. Job executes and calls back to the Django worker (via `WORKER_URL`)
5. Worker processes the callback and updates the database
6. Test polls API until completion
7. Test verifies status and findings

If your environment uses a custom public domain, make sure `WORKER_URL` is set to the worker `*.run.app` URL. Otherwise callbacks may accidentally route to the public domain (which points at the web service via the load balancer) and fail.

### Prerequisites

1. **Deployed staging environment** - Cloud Run web service accessible
2. **API token** - Create one in Settings -> API Tokens
3. **Test workflow** - A workflow with an EnergyPlus validator step

### Running E2E Tests

```bash
# Set environment variables
export E2E_TEST_API_URL=https://your-staging-app.run.app/api/v1
export E2E_TEST_API_TOKEN=your-api-token
export E2E_TEST_WORKFLOW_ID=your-workflow-uuid

# Run E2E tests
just test-e2e

# Or pass variables inline
E2E_TEST_API_URL=https://... E2E_TEST_API_TOKEN=... E2E_TEST_WORKFLOW_ID=... just test-e2e
```

### E2E Test Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `E2E_TEST_API_URL` | Yes | API base URL (e.g., `https://staging.validibot.com/api/v1`) |
| `E2E_TEST_API_TOKEN` | Yes | Valid API token with workflow execution permission |
| `E2E_TEST_WORKFLOW_ID` | Yes | UUID of workflow with EnergyPlus validator step |
| `E2E_TEST_WORKFLOW_EXPECTS_SUCCESS` | No | Set to `false` if test file should fail validation (default: `true`) |

### Creating a Test Workflow

For E2E tests, you need a workflow configured with an EnergyPlus validator step:

1. Log into the staging environment
2. Create a new workflow
3. Add a step with the EnergyPlus validator
4. Note the workflow UUID from the URL
5. Use this UUID for `E2E_TEST_WORKFLOW_ID`

### Test File

The E2E tests use `tests/data/energyplus/example_epjson.json` as the test input. This should be a valid EnergyPlus model that runs quickly.

## Running Tests in CI

For CI/CD pipelines:

```yaml
# Integration tests (skip callbacks)
- name: Run integration tests
  env:
    GCP_PROJECT_ID: ${{ secrets.GCP_PROJECT_ID }}
    GCS_VALIDATION_BUCKET: ${{ secrets.GCS_VALIDATION_BUCKET }}
    # ... other env vars
  run: just test-integration

# E2E tests (post-deployment)
- name: Run E2E tests
  env:
    E2E_TEST_API_URL: https://staging.validibot.com/api/v1
    E2E_TEST_API_TOKEN: ${{ secrets.E2E_API_TOKEN }}
    E2E_TEST_WORKFLOW_ID: ${{ vars.E2E_WORKFLOW_ID }}
  run: just test-e2e
```

## Troubleshooting

### Tests Skip with "missing settings"

Ensure all required environment variables are set. Run `env | grep -E "(GCP|GCS|E2E)"` to check.

### E2E Test Times Out

- Check Cloud Logging for the validation run
- Verify the callback URL is correct and reachable
- Check Cloud Run Job execution status

### Integration Tests Fail on GCS

- Verify GCP credentials: `gcloud auth application-default print-access-token`
- Check bucket exists: `gcloud storage buckets describe gs://your-bucket`

### "Workflow not accessible" Error

- Verify the workflow UUID is correct
- Check the API token has permission for that organization
- Ensure the workflow is not archived

## Related

- [Debug a Run](./debug-a-run.md) - Troubleshooting failed validations
- [Cloud Logging](../google_cloud/logging.md) - Viewing logs
- [Deployment Guide](../google_cloud/deployment.md) - Staging setup

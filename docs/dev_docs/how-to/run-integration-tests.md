# Integration Tests

Integration tests verify that Validibot's cloud integrations work correctly.
They run against real infrastructure - a local Postgres instance, GCS buckets,
Cloud Run Jobs, and Selenium-driven browser tests. Unlike unit tests, they
exercise the actual infrastructure layer but don't test the full HTTP request
lifecycle (that's what [E2E tests](./run-e2e-tests.md) are for).

For an overview of all test layers, see the [Testing Overview](./testing.md).

## What integration tests cover

- Cloud Run Job execution (launching validator containers)
- GCS storage operations (uploading, downloading, signed URLs)
- Selenium browser tests (UI flows with a real browser)
- Database operations against a real Postgres instance

## Prerequisites

- Docker Compose available
- GCP credentials configured (`gcloud auth application-default login`) for
  cloud tests
- Required environment variables (see below)

## Running

```bash
# Run all integration tests
just test-integration

# Run a specific test file
just test-integration tests/tests_integration/test_validator_jobs.py

# Run with extra verbosity
just test-integration -vvv
```

The `just test-integration` recipe:

1. Ensures the `django` Docker image exists (Chromium + chromedriver baked in
   for Selenium)
2. Resets and starts Postgres + Mailpit containers
3. Runs tests inside the Django container
4. Stops containers when done

**Tips:**

- Set `BUILD_DJANGO_IMAGE=1` to force a rebuild after Dockerfile changes
- Set `SELENIUM_HEADLESS=0` to watch Selenium tests in a browser
- If running outside Docker, set `CHROME_BIN` and `CHROMEDRIVER_PATH`

## Required environment variables

For Cloud Run Job tests:

```bash
export GCP_PROJECT_ID=your-project
export GCS_VALIDATION_BUCKET=your-bucket
export GCS_ENERGYPLUS_JOB_NAME=$GCP_APP_NAME-validator-energyplus
export GCS_FMU_JOB_NAME=$GCP_APP_NAME-validator-fmu
export GCP_REGION=us-west1
```

These are typically exported in your shell (often via `source set-env.sh`) or
loaded from `.envs/` in Docker/GCP.

## Cloud E2E tests (staging)

The integration test directory also includes tests that verify the complete
validation flow against a deployed staging environment, including Cloud Run
Job callbacks. These require a deployed environment where Cloud Run Jobs can
call back to Django.

What the cloud E2E flow tests:

1. Submit file via API
2. Django creates ValidationRun
3. Django launches a Cloud Run Job (Jobs API)
4. Job executes and calls back to the Django worker (via `WORKER_URL`)
5. Worker processes the callback and updates the database
6. Test polls API until completion
7. Test verifies status and findings

If your environment uses a custom public domain (via load balancer or domain
mapping), make sure `WORKER_URL` is set to the worker `*.run.app` URL.
Otherwise callbacks may accidentally route to the public domain (which points
at the web service) and fail.

### Running cloud E2E tests

```bash
# Set environment variables
export E2E_TEST_API_URL=https://your-staging-app.run.app/api/v1
export E2E_TEST_API_TOKEN=your-api-token
export E2E_TEST_WORKFLOW_ID=your-workflow-uuid

# Run
just test-e2e
```

| Variable                            | Required | Description                                                          |
| ----------------------------------- | -------- | -------------------------------------------------------------------- |
| `E2E_TEST_API_URL`                  | Yes      | API base URL (e.g., `https://staging.validibot.com/api/v1`)          |
| `E2E_TEST_API_TOKEN`                | Yes      | Valid API token with workflow execution permission                   |
| `E2E_TEST_WORKFLOW_ID`              | Yes      | UUID of workflow with EnergyPlus validator step                      |
| `E2E_TEST_WORKFLOW_EXPECTS_SUCCESS` | No       | Set to `false` if test file should fail validation (default: `true`) |

### Running in CI

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

## psycopg3 + live_server fix

Django's `live_server` fixture uses a threaded WSGI server, but psycopg3
connections aren't thread-safe. After Selenium tests hit the live server,
database connections can become corrupted.

The fix in `tests/tests_integration/conftest.py`:

1. **Autouse fixture** — Resets BAD psycopg3 connections before/after each test
2. **Monkey-patched flush** — Resets connections before Django's teardown flush

Additionally, `config/settings/test.py` sets `CONN_MAX_AGE = 0` to disable
persistent connections. This is a known Django + psycopg3 issue (Django tickets
#32416, #35455).

## Troubleshooting

### Tests skip with "missing settings"

Ensure all required environment variables are set. Run
`env | grep -E "(GCP|GCS|E2E)"` to check.

### E2E test times out

- Check Cloud Logging for the validation run
- Verify the callback URL is correct and reachable
- Check Cloud Run Job execution status

### Integration tests fail on GCS

- Verify GCP credentials: `gcloud auth application-default print-access-token`
- Check bucket exists: `gcloud storage buckets describe gs://your-bucket`

### "Workflow not accessible" error

- Verify the workflow UUID is correct
- Check the API token has permission for that organization
- Ensure the workflow is not archived

## Related

- [Testing Overview](./testing.md) - All test layers and when to use each
- [E2E and Stress Tests](./run-e2e-tests.md) - Local stack stress and
  EnergyPlus simulation tests
- [Debug a Run](./debug-a-run.md) - Troubleshooting failed validations
- [Cloud Logging](../google_cloud/logging.md) - Viewing logs

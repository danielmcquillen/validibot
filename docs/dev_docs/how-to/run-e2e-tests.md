# Running Stress and Multi-Run Tests

Validibot includes two levels of multi-run testing that verify the validation
engine handles repeated and concurrent workloads correctly. They catch
database integrity errors, ID collisions, and lost or duplicated runs.

## In-Process Multi-Run Tests

These tests live in `tests/tests_use_cases/test_multi_run_validation.py` and
submit many validation runs in series via Django's test client. They run as
part of the normal `pytest` suite - no Docker or special setup needed.

What they test:

- Many runs submitted in rapid succession all reach a terminal status
- Each run gets a unique ID (no collisions)
- Results are correct (valid payloads succeed, invalid payloads fail)
- No database integrity errors under load

These tests use `CELERY_TASK_ALWAYS_EAGER=True`, so each run completes
synchronously during the POST call. This means they verify correctness under
rapid serial load but not true HTTP concurrency - that's what the full-stack
tests below are for.

### Running

```bash
# Runs automatically as part of the normal test suite
pytest

# Or target the file directly
pytest tests/tests_use_cases/test_multi_run_validation.py -v
```

## E2E Stress Tests

These tests live in `tests/tests_e2e/` and run against a real, running
Validibot environment. They make real HTTP requests and exercise the complete
stack including Celery dispatch, Redis broker, and worker concurrency.

### Why separate tests?

Normal `pytest` runs use Django's test database and in-process execution, which
is fast and isolated but doesn't catch issues in the real HTTP/Celery/Redis
pipeline. E2E tests catch problems like:

- Celery task dispatch failures under load
- Redis broker bottlenecks
- Worker concurrency issues
- HTTP connection pooling problems

These tests are excluded from normal `pytest` runs (via `norecursedirs` in
`pyproject.toml`) and have their own `just` recipe.

### Running

Test data (user, org, workflow, API token) is auto-provisioned by the
`setup_fullstack_test_data` management command, which `just test-e2e`
calls automatically. All you need is a running environment:

```bash
just up
just test-e2e
```

That's it. The recipe provisions a test user, organization, workflow with a
JSON Schema validation step, and an API token on the running instance.

### Pointing at a different environment

You can override the auto-provisioned values to test against a deployed staging
environment:

```bash
FULLSTACK_API_URL=https://staging.example.com/api/v1 \
FULLSTACK_API_TOKEN=your-token \
FULLSTACK_ORG_SLUG=my-org \
FULLSTACK_WORKFLOW_ID=workflow-uuid \
just test-e2e
```

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FULLSTACK_API_URL` | No | `http://localhost:8000/api/v1` | Base API URL |
| `FULLSTACK_API_TOKEN` | No | Auto-provisioned | API bearer token |
| `FULLSTACK_ORG_SLUG` | No | Auto-provisioned | Organization slug |
| `FULLSTACK_WORKFLOW_ID` | No | Auto-provisioned | Workflow UUID |

When any of these are set manually, auto-provisioning is skipped.

### What the tests verify

- Submit 10 concurrent validation runs via real HTTP
- All runs reach a terminal status (no runs lost)
- No duplicated run IDs
- Identical payloads produce consistent results under concurrent load

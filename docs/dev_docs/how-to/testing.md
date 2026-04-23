# Testing Overview

Validibot uses a layered testing strategy. Each layer runs at a different level
of the stack and catches different kinds of bugs. Understanding what each layer
does helps you pick the right test type when adding new functionality and know
which suite to run when debugging a failure.

## Test Layers at a Glance

| Layer | Location | What it exercises | How to run | Speed |
|-------|----------|-------------------|------------|-------|
| **Unit** | `validibot/*/tests/` | Individual functions, classes, models | `pytest` | Fast (seconds) |
| **Use case** | `tests/tests_use_cases/` | Business scenarios end-to-end in-process | `pytest` | Fast (seconds) |
| **Integration** | `tests/tests_integration/` | Cloud infra (GCS, Cloud Run Jobs, Selenium) | `just local test-integration` | Medium (minutes) |
| **E2E stress** | `tests/tests_e2e/test_stress.py` | Concurrent HTTP load against live stack | `just local test-e2e` | Medium (minutes) |
| **E2E workflow** | `tests/tests_e2e/test_energyplus_template.py` | Real Docker-based simulations, full pipeline | `just local-cloud e2e-tests` | Slow (2-5 min/test) |

## How the layers differ

**Unit and use case tests** run with `CELERY_TASK_ALWAYS_EAGER=True` and
Django's test database. They're fast and isolated but don't exercise the real
HTTP/Celery/Redis pipeline. They run automatically as part of the normal
`pytest` suite.

**Integration tests** run against real infrastructure - a local Postgres
instance, GCS buckets, Cloud Run Jobs. They verify that Validibot's cloud
integrations work correctly but don't test the full request lifecycle. They
require Docker and (for cloud tests) GCP credentials.

**E2E tests** run against a fully running Validibot environment (Docker Compose
stack). They make real HTTP requests to the API and exercise the complete
pipeline: API submission, Celery task dispatch, Redis broker, worker execution,
and (for advanced validators) Docker container simulation. These catch problems
that unit and integration tests can't: task dispatch failures, broker
bottlenecks, concurrency bugs, and end-to-end data flow issues.

## Which tests to run when

| Scenario | Run |
|----------|-----|
| Routine development | `pytest` (unit + use case) |
| Changed models, views, or business logic | `pytest` |
| Changed cloud integration code (GCS, Cloud Run) | `just local test-integration` |
| Changed Celery tasks or worker code | `just local test-e2e` |
| Changed EnergyPlus validator or template pipeline | `just local-cloud e2e-tests` |
| Before opening a PR | `pytest` + `pre-commit run --all-files` |
| Verifying a deployed environment | `just local test-e2e` with env vars pointing at staging |

## Running the test suite

```bash
# Unit + use case tests (the everyday default)
pytest

# With coverage
pytest --cov=validibot

# A specific app's tests
pytest validibot/validations/tests/

# A specific test file
pytest validibot/validations/tests/test_models.py -v
```

E2E and integration tests are excluded from the default `pytest` run via
`norecursedirs` in `pyproject.toml`. Each has its own `just` recipe documented
in its dedicated guide.

## Detailed guides

- **[Integration Tests](./run-integration-tests.md)** — Cloud Run Jobs, GCS,
  Selenium browser tests
- **[E2E and Stress Tests](./run-e2e-tests.md)** — Concurrent stress tests and
  EnergyPlus simulation tests against a live stack

## Writing tests

A few conventions to follow when adding new tests:

- **Every test file** needs a module-level docstring explaining what the suite
  covers and why.
- **Every test method** needs a docstring explaining *why* the test matters, not
  just what it does.
- **Always include security tests** for features that handle user input. Test
  for XSS, injection, XXE, and other OWASP-style attacks.
- **Use range assertions for simulation outputs.** EnergyPlus and FMU results
  vary across versions and platforms, so assert within a reasonable range rather
  than exact values.
- Tests follow the code: commercial feature tests live in this repo (gated by
  feature flags), not in the commercial repos.

See [AGENTS.md](../../AGENTS.md) for the full coding standards.

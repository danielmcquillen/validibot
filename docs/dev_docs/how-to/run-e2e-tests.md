# Running E2E and Stress Tests

Validibot includes several levels of end-to-end and stress testing. These range
from quick in-process tests that run as part of the normal `pytest` suite, to
full-stack tests that exercise real Docker-based simulations against a running
Validibot environment.

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

These tests live in `tests/tests_e2e/test_stress.py` and run against a real,
running Validibot environment. They make real HTTP requests and exercise the
complete stack including Celery dispatch, Redis broker, and worker concurrency.

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

## EnergyPlus E2E Tests

These tests live in `tests/tests_e2e/test_energyplus_template.py` and run a
**real EnergyPlus simulation** in Docker against the local Docker Compose stack.
They reproduce the exact scenario from the blog post "Validating With EnergyPlus
- Window Glazing Analysis": submit JSON parameter values via the API, wait for
the EnergyPlus simulation to complete, and verify output signals and assertion
results.

Each test takes 2-5 minutes because it runs a real EnergyPlus simulation inside
a Docker container.

### What the tests verify

- **Passing scenario**: A well-insulated window (U=1.70, SHGC=0.25) passes all
  output assertions (heat loss under 800 kWh, heating-dominated)
- **Failing scenario**: A poorly-insulated window (U=6.00) runs the simulation
  but fails the heat loss assertion (over 800 kWh)
- **Input validation**: An out-of-range U-Factor (10.0, exceeds max 7.0) is
  rejected before the simulation even starts

### Running

The simplest way to run the EnergyPlus E2E tests is a single command that
handles everything - starting Docker, provisioning test data, and running the
tests:

```bash
just local-cloud e2e-tests
```

This command automatically:

1. Checks the Docker Compose stack is running (starts it if needed)
2. Verifies the EnergyPlus validator Docker image exists
3. Runs prerequisite setup (`setup_validibot`, `seed_weather_files`)
4. Provisions the E2E workflow via `setup_e2e_workflows`
5. Runs the pytest suite

You can pass additional pytest arguments:

```bash
# Run only the passing scenario test
just local-cloud e2e-tests -k test_passing

# Run with extra debug output
just local-cloud e2e-tests --log-cli-level=DEBUG
```

### Prerequisites

The `just local-cloud e2e-tests` recipe handles most prerequisites
automatically, but you do need to build the EnergyPlus validator Docker image
first (one-time step):

```bash
# In the validibot-validators repo
cd ../validibot-validators
just build energyplus
```

### How it works

The test workflow is provisioned by the `setup_e2e_workflows` management
command, which creates:

- A workflow that accepts JSON parameter values
- A workflow step with an EnergyPlus parameterized IDF template
  (`tests/assets/idf/window_glazing_template.idf`)
- Template variable definitions with bounds (U_FACTOR 0.1-7.0, SHGC 0.01-0.99,
  VISIBLE_TRANSMITTANCE 0.01-0.99)
- Two CEL output assertions: `window_heat_loss_kwh < 800` and
  `cooling_energy_kwh < heating_energy_kwh`
- A weather file reference (San Francisco TMY3)

The tests submit JSON payloads via the API, which triggers the full validation
pipeline: template substitution, EnergyPlus simulation in Docker, output signal
extraction, and CEL assertion evaluation.

### Adding new E2E workflow tests

The framework is designed to be extended. To add a new E2E scenario (e.g. FMU,
another EnergyPlus workflow):

1. Add a `_ensure_*_workflow()` method to `setup_e2e_workflows.py` and register
   it in `handle()`
2. Add an `E2E_{SCENARIO}_WORKFLOW_ID` fixture to `tests/tests_e2e/conftest.py`
3. Create a new test file in `tests/tests_e2e/` using the helpers from
   `tests/tests_e2e/helpers.py` (submit/poll/assert pattern)
4. Add a `just` recipe if the scenario needs different prerequisites

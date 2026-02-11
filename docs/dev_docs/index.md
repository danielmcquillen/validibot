# Developer Documentation

This documentation is for developers working on Validibot itself. If you're looking to use Validibot as an end user, see the [User Guide](https://docs.validibot.com/) instead.

---

## Getting Started

New to the codebase? Start here:

1. **[Platform Overview](overview/platform_overview.md)** — What Validibot is and the problems it solves
2. **[How It Works](overview/how_it_works.md)** — Technical walkthrough of the validation lifecycle
3. **[Quick Reference](quick_reference.md)** — Core concepts and basic usage patterns
4. **[Docker Setup](docker.md)** — Run Validibot locally with Docker

---

## Architecture

Understand how the system is built:

- **[Workflow Engine](overview/workflow_engine.md)** — How ValidationRunService orchestrates steps
- **[Step Processor](overview/step_processor.md)** — The processor pattern for validator execution
- **[Submission Modes](overview/request_modes.md)** — How API payload shapes are detected
- **[Settings Reference](overview/settings.md)** — Environment variables and feature flags
- **[Dashboard](dashboard.md)** — Architecture and extension points for the dashboard module
- **[Related Libraries](related_libraries.md)** — How `validibot_shared` connects to this project

---

## How-To Guides

Step-by-step instructions for common tasks:

- **[Using a Workflow via the API](how-to/use-workflow.md)** — Submit data programmatically
- **[Author Workflow Steps](how-to/author-workflow-steps.md)** — Configure validation steps in the UI
- **[Configure Storage](how-to/configure-storage.md)** — Set up file storage backends
- **[Configure Scheduled Tasks](how-to/configure-scheduled-tasks.md)** — Set up background jobs
- **[Add a Form](how-to/add-a-form.md)** — Django Crispy Forms patterns
- **[Manage Organizations & Projects](organization_management.md)** — Admin workflows

---

## Data Model

The entities that make up Validibot:

- **[Data Model Overview](data-model/index.md)** — Core entities and relationships
- **[Projects](data-model/projects.md)** — Organization-scoped namespaces
- **[Submissions](data-model/submissions.md)** — Content being validated
- **[Runs](data-model/runs.md)** — Validation execution tracking
- **[Steps](data-model/steps.md)** — Individual validation operations
- **[Findings](data-model/findings.md)** — Validation results and issues
- **[Users & Roles](data-model/users_roles.md)** — Organization membership
- **[Deletions](data-model/deletions.md)** — How deletions are managed

---

## Deployment

Deploy Validibot to production:

- **[Deployment Overview](deployment/overview.md)** — Environments and release workflow
- **[Google Cloud](google_cloud/deployment.md)** — Deploy to Cloud Run
- **[Docker Compose](deployment/docker-compose-responsibility.md)** — Docker Compose deployments
- **[Scheduled Jobs (GCP)](google_cloud/scheduled-jobs.md)** — Cloud Scheduler setup
- **[Scheduled Tasks (Docker Compose)](how-to/configure-scheduled-tasks.md)** — Celery + Celery Beat
- **[Go-Live Checklist](deployment/go-live-checklist.md)** — Pre-launch tasks
- **[Important Notes](deployment/important_notes.md)** — Common deployment gotchas

---

## Integrations

- **[EnergyPlus Modal](integrations/energyplus_modal.md)** — Modal-backed EnergyPlus simulation runner

---

## Marketing

- **[Homepage Waitlist](marketing/homepage.md)** — Beta waitlist card
- **[Feature Pages](marketing/features.md)** — Messaging guide for marketing content

---

## Testing

Run the test suite with `uv run --extra dev pytest`. Integration tests require Docker.

### Integration Tests

Pytest ignores `tests_integration` by default. Use `just test-integration` for the end-to-end suite, which:

1. Ensures the `django` Docker image exists (Chromium + chromedriver baked in for Selenium)
2. Resets and starts Postgres + Mailpit containers
3. Runs tests inside the Django container
4. Stops containers when done

```bash
# Manual equivalent
docker compose -f docker-compose.local.yml down -v
docker compose -f docker-compose.local.yml up -d postgres mailpit
docker compose -f docker-compose.local.yml run --rm \
  -e DJANGO_SETTINGS_MODULE=config.settings.test \
  django uv run --extra dev pytest tests/tests_integration/ -v
docker compose -f docker-compose.local.yml stop postgres mailpit
```

**Tips:**

- Set `BUILD_DJANGO_IMAGE=1` to force a rebuild after Dockerfile changes
- Set `SELENIUM_HEADLESS=0` to watch Selenium tests in a browser
- If running outside Docker, set `CHROME_BIN` and `CHROMEDRIVER_PATH`

### psycopg3 + live_server Fix

Django's `live_server` fixture uses a threaded WSGI server, but psycopg3 connections aren't thread-safe. After Selenium tests hit the live server, database connections can become corrupted.

The fix in `tests/tests_integration/conftest.py`:

1. **Autouse fixture** — Resets BAD psycopg3 connections before/after each test
2. **Monkey-patched flush** — Resets connections before Django's teardown flush

Additionally, `config/settings/test.py` sets `CONN_MAX_AGE = 0` to disable persistent connections.

This is a known Django + psycopg3 issue (Django tickets #32416, #35455).

# Validibot Documentation

Welcome to the **Validibot** documentation.

This site describes the core concepts, data model, and API for working with validation workflows.

## Contents

- [Quick Reference](#quick-reference)
- [Overview](#overview)
- [How-To Guides](#how-to-guides)
- [Marketing](#marketing)
- [Architecture Decision Records](#architecture-decision-records)
- [Deployment](#deployment)
- [Data Model](#data-model)

## Quick Reference

- [Quick Reference](quick_reference.md) - Summary of core concepts and basic usage
- [Related Libraries](related_libraries.md) - How `vb_shared` and `sv_modal` connect to Validibot

### Overview

- [Platform Overview](overview/platform_overview.md) - What Validibot is and why it exists
- [How It Works](overview/how_it_works.md) - Detailed technical walkthrough of the validation process
- [Settings Reference](overview/settings.md) - Environment and feature flags that shape behaviour locally and in prod
- [Working Agreements](overview/platform_overview.md#working-agreements-for-developers) - How we keep the project understandable
- [Submission Modes](overview/request_modes.md) - How API payload shapes are detected
- [Dashboard](dashboard.md) - Architecture and extension points for the dashboard module

### How-To Guides

- [Using a Workflow via the API](how-to/use-workflow.md) - Step-by-step API integration guide
- [Author Workflow Steps](how-to/author-workflow-steps.md) - Configure validation templates via the UI wizard
- [Manage Organizations & Projects](organization_management.md) - Admin workflows for organizations and projects
- [Configure the Badge JWKS Endpoint](how-to/configure-jwks.md) - Publish KMS-backed signing keys

### Marketing

- [Homepage Waitlist](marketing/homepage.md) - Structure and automation details for the beta waitlist card
- [Feature Pages](marketing/features.md) - Messaging guide for the marketing feature content

### Architecture Decision Records

- [Architecture Decision Records](adr/index.md) - Directory of decisions that guide the platform

### Testing

Pytest ignores `tests_integration` by default (see `pyproject.toml`). Use `just test-integration` for the end-to-end suite; it will:

- ensure the `django` image exists (Chromium + chromedriver are baked in for Selenium UI flows). Set `BUILD_DJANGO_IMAGE=1` if you need to force a rebuild after changing the Dockerfile.
- reset and start Postgres + Mailpit (`docker compose -f docker-compose.local.yml down -v && ... up -d postgres mailpit`)
- run the tests inside the Django container (service DNS `postgres` resolves; no host browser/driver needed):
  `docker compose -f docker-compose.local.yml run --rm -e DJANGO_SETTINGS_MODULE=config.settings.test django uv run --extra dev pytest tests/tests_integration/ -v --log-cli-level=INFO`
- stop the containers when done

Notes:
- Selenium login tests run headless by default. Set `SELENIUM_HEADLESS=0` if you want to watch the browser.
- If you are running outside Docker for some reason, you must provide a working Chrome/Chromedriver pair and set `CHROME_BIN`/`CHROMEDRIVER_PATH`, or the tests will fail fast with a clear error.

Manual equivalent:
```
docker compose -f docker-compose.local.yml down -v
# Optional rebuild if you changed Dockerfile/deps:
# BUILD_DJANGO_IMAGE=1 docker compose -f docker-compose.local.yml build django
docker compose -f docker-compose.local.yml up -d postgres mailpit
docker compose -f docker-compose.local.yml run --rm -e DJANGO_SETTINGS_MODULE=config.settings.test django uv run --extra dev pytest tests/tests_integration/ -v --log-cli-level=INFO
docker compose -f docker-compose.local.yml stop postgres mailpit
```

#### psycopg3 + live_server threading fix

Django's `live_server` fixture runs a threaded WSGI server. psycopg3 connections are **not** thread-safe, so after a Selenium test makes HTTP requests to the live server, the database connection can become corrupted (status = BAD). Django's `DatabaseWrapper` still holds a reference to this dead connection, and when pytest-django tries to flush the database during teardown, it fails with `OperationalError: the connection is closed`.

The fix lives in [tests/tests_integration/conftest.py](../../tests/tests_integration/conftest.py):

1. **Autouse fixture** - Resets any BAD psycopg3 connections before and after each test
2. **Monkey-patched flush command** - Resets connections before Django's flush runs during teardown

When resetting a BAD connection, we must also clear Django's internal state (`closed_in_transaction`, `in_atomic_block`, `savepoint_ids`, `needs_rollback`) or Django will refuse to create new connections.

Additionally, [config/settings/test.py](../../config/settings/test.py) sets `CONN_MAX_AGE = 0` to disable persistent connections for tests.

This is a known Django + psycopg3 incompatibility (see Django tickets #32416, #35455).

### Deployment

- [Deployment Overview](deployment/overview.md) - Environments, release workflow, and operational checklist
- [Heroku Deployment](deployment/heroku.md) - Step-by-step commands for the Heroku app

### Data Model

- [Data Model Overview](data-model/index.md) - Core entities and relationships
- [Projects & Context](data-model/projects.md) - Organization-scoped namespaces and propagation rules
- [Submissions](data-model/submissions.md) - Content submission and storage
- [Runs](data-model/runs.md) - Validation execution and tracking
- [Steps](data-model/steps.md) - Individual validation operations
- [Findings](data-model/findings.md) - Validation results and issues
- [Users & Roles](data-model/users_roles.md) - Organization membership and permissions
- [Deletions](data-model/deletions.md) - How deletions are managed

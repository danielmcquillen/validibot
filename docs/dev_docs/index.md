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
- **[Commercial Extensions](overview/commercial_extensions.md)** — How Pro and Enterprise packages plug in

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
- **[Results](data-model/results.md)** — Findings, artifacts, and summaries
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

## Testing

Run the test suite with `uv run --group dev pytest`. Integration and E2E tests
have their own `just` recipes.

See **[Testing Overview](how-to/testing.md)** for the full testing strategy,
including when to use each test layer and detailed guides for integration,
stress, and EnergyPlus E2E tests.

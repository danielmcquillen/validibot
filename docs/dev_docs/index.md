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
- [Related Libraries](related_libraries.md) - How `sv_shared` and `sv_modal` connect to Validibot

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

Pytest ignores `tests_integration` by default (see `pyproject.toml`). Run them manually when you need the end-to-end checks:

```sh
uv run --extra dev pytest tests_integration
```

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

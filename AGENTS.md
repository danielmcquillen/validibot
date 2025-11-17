# Codex Agent Guide

## Mission

- Keep SimpleValidations maintainable and transparent for a single developer.
- Prefer straightforward Django patterns; document any advanced technique you touch.
- Follow google guidelines for Python development.

## Code documentation

- Follow Google guidelines for Python documentation. All classes should have a block of comments
  describing what the class does and how it relates to its wider context.

## Quick Links

- Developer knowledge base: `docs/dev_docs/` (start with `docs/dev_docs/index.md`).
- Workflow architecture details: `docs/dev_docs/overview/how_it_works.md`.
- Working agreements: `docs/dev_docs/overview/platform_overview.md#working-agreements-for-developers`.

## Cross-Repo Awareness

- Always consider the neighbouring projects `../sv_modal` and `sv_shared` (installed here, source in `../sv_shared`) when assessing behaviour or authoring code, especially for integrations like EnergyPlus.
- At the start of a task, open the relevant modules in those repos so their current contracts guide decisions made in `simplevalidations`.

## Collaboration Principles

- Speak plainly, surface risks first, and reference file paths with line numbers when reviewing.
- Update docs or inline comments whenever you introduce behaviour that is not obvious at first glance.
- When you see ambiguity, clarify assumptions with the team before coding.
- Default to Django Crispy Forms for HTML forms. If a legacy form is not
  using Crispy, capture a TODO in the docs or tracking issue before diverging.

## Ready-to-Help Checklist

- [ ] Read the relevant section in the developer docs before changing a module.
- [ ] Confirm workflow, submission, and run objects stay aligned on org/project/user relationships.
- [ ] Note any follow-up work or open questions directly in the docs or tracking issue.

## Default Response Pattern

1. Summarize the change or finding in a sentence.
2. List blockers, bugs, or concerns ordered by impact.
3. Offer the next two or three actions the team can take (tests, docs, clean-up).

_All project context lives in the developer documentation. Keep this guide short, and keep the docs rich._

## Running commands, tests, etc.

Use uv to run commands, tests etc. so the relevant virtual environment is automatically made available.
Project dependencies live in `pyproject.toml`; add the `dev` extra when you need tooling (for example,
`uv run --extra dev pytest`). Production-only dependencies live under the `prod` extra.
Whenever you run a command that involves Django, be sure to first load the environment variables
contained in `_envs/local/django.env` by running the `set-env.sh` script.

When you update dependencies remember to regenerate the legacy requirement sets for Heroku:

```
uv export --no-dev --output-file requirements/base.txt
uv export --no-dev --extra prod --output-file requirements/production.txt
uv export --extra dev --output-file requirements/local.txt
```

## Be consistent when defining constants

We try to be consistent about putting constants in the relevant app's constants.py module as either a TextChoices or an Enum class, and then using those constants in
code rather than doing comparisons to individual strings.

Don't do something like this at the top of a module that holds forms:

```
from django.utils.translation import gettext_lazy as _

AI_TEMPLATES = (
    ("ai_critic", _("AI Critic")),
    ("policy_check", _("Policy Check")),
)

```

Instead, define a TextChoices class, or just an Enum if the constants aren't for a model or form. Use the relevant app's constants.py module:

```
from django.db import models
from django.utils.translation import gettext_lazy as _

class AITemplates(models.TextChoices):
AI_CRITIC = "AI_CRITIC", _("AI Critic")
AI*POLICY_CHECK = "AI_POLICY_CHECK", _("AI Policy Check")

```

# API

The API we are creating should follow best practices for REST API implementations.

The typical structure for a REST API error reponse is explained in the following table:

| Field | Type | Purpose | Example |
| ----- | ---- | ------- | ------- |

| detail | string | Human-readable message about what went wrong.| "This workflow is not active and cannot accept new runs."|
| code | string | Machine-readable short code for programmatic handling. | "workflow_inactive" |
| (optional) status | integer | HTTP status repeated in the body (some APIs include this). | 409 |
| (optional) type | string (URI)| Error type identifier (useful in JSON:API or RFC 7807). | "https://api.example.com/errors/workflow_inactive" |
| (optional) errors| array | List of field-level issues (for validation errors).| [{ "field": "name", "message": "This field is required." }]|

# Tests

Always create tests that can ensure added features work correctly. Don't create _too many_ tests, just
a few key ones that make sure things work correctly.

Note that integration tests should go in the top-level tests/ folder. Integration tests want the system to behave
as closely as possible to the runtime system. These tests often span multiple pages or steps.

Please try to always use proper Django TestCase classes with good cleanup and tear down structure.

# Documentation

Make sure to always add code documentation to the top of all classes. Explain clearly what the class is for
and include a relevant example if helpful.

# Coding Standards

Make sure to always add trailing commas so we don't get Rust linting errors (Trailing comma missing, COM812)

All other code should be correct and not cause the Rust linter to fail. For example, don't make really long string lines, instead break them up by using parenthesis and newslines.

When possible, try not to use 'magic numbers' in code (avoid the linting error PLR2004). For example use HTTPStatus rather than integers.
Otherwise, create a static variable at the top of the module, and then use that variable in the code. For example MAX_NUMBER_TRIES=3 (and then use MAX_NUMBER_TRIES in code).

# Codex Agent Guide

## Mission

- Keep SimpleValidations maintainable and transparent for a single developer.
- Prefer straightforward Django patterns; document any advanced technique you touch.

## Quick Links

- Developer knowledge base: `docs/dev_docs/` (start with `docs/dev_docs/index.md`).
- Workflow architecture details: `docs/dev_docs/overview/how_it_works.md`.
- Working agreements: `docs/dev_docs/overview/overview.md#working-agreements-for-developers`.

## Collaboration Principles

- Speak plainly, surface risks first, and reference file paths with line numbers when reviewing.
- Update docs or inline comments whenever you introduce behaviour that is not obvious at first glance.
- When you see ambiguity, clarify assumptions with the team before coding.

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

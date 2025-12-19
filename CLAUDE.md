# Validibot - Project Context for Claude Code

## Project Overview

Validibot is a Django-based data validation engine that helps users validate building energy models and other technical data. The project uses Django 5.2, Python 3.13, Bootstrap 5, HTMx for dynamic interactions, and runs on Google Cloud Platform.

## Critical: Always Check AGENTS.md

**IMPORTANT**: Before working on any code in this codebase, always read and follow the guidelines in [AGENTS.md](AGENTS.md). That file contains essential coding standards, patterns, and conventions that must be followed for this project.

## Project Structure

- `validibot/` - Main Django application code
- `config/` - Django settings and configuration
- `docs/dev_docs/` - Developer documentation (start with `docs/dev_docs/index.md`)
- `docs/user_docs/` - End-user documentation
- `tests/` - Integration tests
- `.envs/` - Local (Docker + host-run) and cloud deployment environment files
- `vb_shared_dev/` - Symlink to shared library code (../vb_shared)
- `justfile` - Command runner (similar to Makefile)

## Tech Stack

- **Backend**: Django 5.2.8, Python 3.13
- **Database**: PostgreSQL (via psycopg3)
- **Frontend**: Bootstrap 5, HTMx, Chart.js
- **Task Queue**: Cloud Tasks / Cloud Run Jobs (Celery removed)
- **Package Management**: uv (fast Python package manager)
- **Linting**: Ruff
- **Type Checking**: mypy with django-stubs
- **Testing**: pytest with pytest-django
- **Documentation**: MkDocs with Material theme

## Cross-Repo Dependencies

This project works alongside two related repositories:

1. **vb_shared** - Shared library for integrations (EnergyPlus, FMI, etc.)
   - Installed from Git in production
   - Symlinked to `vb_shared_dev/` for local development
2. **vb_validators** - Cloud Run Job validator containers
   - Located at `../vb_validators`
   - Depends on vb_shared via Git URL

Always consider these neighboring projects when working on integrations or modifying shared functionality.

### vb_shared Workflow

**IMPORTANT**: When making changes to vb_shared that other repos depend on (vb_validators, validibot):

1. Make changes in `../vb_shared`
2. Commit and push to GitHub
3. Run `uv sync` in dependent repos to pull the new version

Do NOT use local path overrides in pyproject.toml (`tool.uv.sources`) - this breaks Docker builds. Always go through Git.

## Environment Setup

### Using uv (Required)

All commands must be run with `uv` to ensure correct virtual environment:

```bash
# Run Django commands (requires environment variables)
source set-env.sh && uv run python manage.py [command]

# Run tests
uv run --extra dev pytest

# Run linter
uv run --extra dev ruff check

# Run type checker
uv run --extra dev mypy validibot
```

### Environment Files

| Directory | Purpose                          | Used By                                |
| --------- | -------------------------------- | -------------------------------------- |
| `.envs/`  | Local + Docker + cloud deployments | Docker Compose, `set-env.sh`, GCP Secret Manager |

**Important:** Deployment secrets live under `.envs/.production/` and are uploaded to GCP Secret Manager; keep them private and out of git.

## Django Conventions

### Forms

- Default to Django Crispy Forms with crispy-bootstrap5
- Use Bootstrap 5 styling
- See `docs/dev_docs/how-to/add-a-form.md` for detailed guidance

### HTMx + Bootstrap Modal Pattern

- Follow the two-template pattern documented in AGENTS.md
- Use `innerHTML` swap, never `outerHTML` on modals
- Return 200 status for validation errors (not 400)
- Implement GET handler for fresh form content

### Constants

- Define constants in `constants.py` using `TextChoices` or `Enum`
- Never use string literals for comparisons

### Code Documentation

- Follow Google Python Style Guide
- All classes must have docstrings explaining purpose and context
- Include examples where helpful

## Testing Guidelines

- Use proper Django TestCase classes with setup/teardown
- Add docstrings to test methods explaining what's being tested
- Integration tests go in `tests/` folder
- Unit tests go in app-specific `tests/` folders
- Don't over-test - focus on key functionality

## Coding Standards

- **Documentation**: All classes need docstrings explaining purpose and context (Google style)
- **Code comments**: Explain _why_ and _how it relates to wider context_, not just _what_
- **Settings files**: Add explanatory comments for non-obvious configuration
- Run `uv run --extra dev ruff check` before finishing any code changes
- Always include trailing commas (avoid COM812 lint error)
- No magic numbers - use HTTPStatus or define constants
- Break up long strings with parentheses and newlines
- Follow Python import order (ruff handles this)
- **Prefer absolute imports** over relative imports (e.g., `from mypackage.module import foo` not `from .module import foo`)

## Documentation Style

- Write in clear, friendly, conversational style
- Use short paragraphs instead of dense bullet lists
- Avoid jargon when a plainer phrase works
- Favor clarity over terseness
- See AGENTS.md "Documentation tone" section for full guidelines

## Git Workflow

- **NEVER** commit, stage, or push unless explicitly asked
- Main branch: `main`
- Current branch: `deploy-target-google`

## API Development

- Follow REST best practices
- Error responses should include:
  - `detail`: Human-readable message
  - `code`: Machine-readable error code
  - Optional: `status`, `type`, `errors` array

## Documentation

The project has two MkDocs sites:

1. **Developer docs**: `mkdocs.dev.yml` (port 9000)
   - For contributors and maintainers
2. **User docs**: `mkdocs.user.yml` (port 9001)
   - For end users

Always update relevant documentation when adding features or changing behavior.

## Key Files to Reference

- [AGENTS.md](AGENTS.md) - **Must read** coding standards and patterns
- `docs/dev_docs/index.md` - Developer knowledge base entry point
- `docs/dev_docs/overview/how_it_works.md` - Workflow architecture
- `docs/dev_docs/overview/platform_overview.md` - Working agreements
- `docs/dev_docs/dependency-management.md` - How to add dependencies

## Working Style

- Keep code maintainable for a single developer
- Prefer straightforward Django patterns over clever solutions
- Document any advanced techniques
- Surface risks and blockers first
- Reference file paths with line numbers when discussing code
- Write documentation in clear, conversational style

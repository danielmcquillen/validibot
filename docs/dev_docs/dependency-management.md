# Dependency Management

Validibot uses [uv](https://docs.astral.sh/uv/) for Python dependency management. All dependencies are declared in `pyproject.toml` and locked in `uv.lock`. This approach provides reproducible builds and fast installs.

## Quick Reference

| Task                             | Command                                          |
| -------------------------------- | ------------------------------------------------ |
| Install all deps (dev)           | `uv sync --extra dev`                            |
| Install prod deps only           | `uv sync`                                        |
| Add a base dependency            | `uv add <package>`                               |
| Add a dev-only dependency        | `uv add --group dev <package>`                   |
| Add a production-only dependency | `uv add --optional prod <package>`               |
| Upgrade a package                | `uv lock --upgrade-package <package> && uv sync` |
| Run a command                    | `uv run python manage.py <command>`              |
| Run tests                        | `uv run --extra dev pytest`                      |

## Dependency Categories

We organize dependencies into three groups:

### Base Dependencies (both local and production)

These are the core packages needed to run the application. They go in the main `[project.dependencies]` section of `pyproject.toml`.

```bash
# Add a new base dependency
uv add django-extensions

# Add with a specific version
uv add "celery>=5.5.0,<6.0"
```

### Dev-Only Dependencies

Development tools like pytest, mypy, and linters. These go in the `[dependency-groups.dev]` section and are only installed when you use `--extra dev`.

```bash
# Add a dev-only dependency
uv add --group dev pytest-cov

# Add multiple at once
uv add --group dev "ruff>=0.14" "mypy>=1.18"
```

### Production-Only Dependencies

Packages only needed in production (like Sentry, Gunicorn workers). These use the `[project.optional-dependencies.prod]` section.

```bash
# Add a production-only dependency
uv add --optional prod sentry-sdk
```

## Installing Dependencies

### For Local Development

```bash
# Install base + dev dependencies
uv sync --extra dev
```

### For Production

```bash
# Install base + production dependencies
uv sync --extra prod
```

### For Docker Builds

The Dockerfile runs `uv sync --extra dev --frozen` which installs from the locked versions without updating the lock file.

## Upgrading Dependencies

### Upgrade a Specific Package

```bash
uv lock --upgrade-package django && uv sync
```

### Upgrade All Packages

```bash
uv lock --upgrade && uv sync
```

### Check for Outdated Packages

```bash
uv pip list --outdated
```

## Legacy Requirements Files (Heroku)

Heroku still reads from `requirements/*.txt` files. After modifying dependencies in `pyproject.toml`, regenerate these files:

```bash
uv export --no-dev --output-file requirements/base.txt
uv export --no-dev --extra prod --output-file requirements/production.txt
uv export --extra dev --output-file requirements/local.txt
```

These files are checked into version control and should be updated whenever `pyproject.toml` or `uv.lock` changes.

## Working with sv_shared

The `sv-shared` package is a sibling project installed as an editable local dependency for development. In `pyproject.toml`:

```toml
[tool.uv.sources]
sv-shared = { path = "sv_shared_dev", editable = true }
```

The `sv_shared_dev` directory is a symlink to `../sv_shared`. When `sv_shared` changes:

1. Make changes in `../sv_shared`
2. Bump the version in `sv_shared/pyproject.toml`
3. Push the changes
4. In this project, run: `uv lock --upgrade-package sv-shared && uv sync`

For Docker, the real `sv_shared` is volume-mounted at runtime (see `docker-compose.local.yml`).

## Common Workflows

### Starting a New Feature

```bash
# Pull latest, sync dependencies
git pull
uv sync --extra dev

# Load environment variables
source set-env.sh

# Run tests to verify setup
uv run pytest
```

### Adding a New Library

```bash
# Add the dependency
uv add requests

# Regenerate Heroku requirements
uv export --no-dev --output-file requirements/base.txt

# Commit both pyproject.toml, uv.lock, and requirements/base.txt
```

### Updating After a Merge

```bash
# If pyproject.toml or uv.lock changed
uv sync --extra dev
```

## Troubleshooting

### "Package not found" After Install

Make sure you're using `uv run` to execute commands:

```bash
# Wrong - uses system Python
python manage.py runserver

# Right - uses uv's managed environment
uv run python manage.py runserver
```

### Lock File Conflicts

If you get conflicts in `uv.lock` after a merge:

```bash
# Accept theirs and regenerate
git checkout --theirs uv.lock
uv lock
```

### Dependency Resolution Errors

Try clearing the cache:

```bash
uv cache clean
uv lock
```

## Reference

- [uv Documentation](https://docs.astral.sh/uv/)
- [pyproject.toml Specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/)

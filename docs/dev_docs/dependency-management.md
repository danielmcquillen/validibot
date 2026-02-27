# Dependency Management

Validibot uses [uv](https://docs.astral.sh/uv/) for Python dependency management. All dependencies are declared in `pyproject.toml` and locked in `uv.lock`. This approach provides reproducible builds and fast installs.

## Quick Reference

| Task                             | Command                                          |
| -------------------------------- | ------------------------------------------------ |
| Install all deps (dev)           | `uv sync --group dev`                            |
| Install prod deps only           | `uv sync`                                        |
| Add a base dependency            | `uv add <package>`                               |
| Add a dev-only dependency        | `uv add --group dev <package>`                   |
| Add an optional extra            | `uv add --optional cloud <package>`              |
| Upgrade a package                | `uv lock --upgrade-package <package> && uv sync` |
| Run a command                    | `uv run python manage.py <command>`              |
| Run tests                        | `uv run --group dev pytest`                      |

## Dependency Categories

We organize dependencies into three groups:

### Base Dependencies (both local and production)

These are the core packages needed to run the application. They go in the main `[project.dependencies]` section of `pyproject.toml`.

```bash
# Add a new base dependency
uv add django-extensions

# Add with a specific version
uv add "httpx>=0.28.0"
```

### Dev-Only Dependencies

Development tools like pytest, mypy, and linters. These go in the `[dependency-groups]` section and are only installed when you use `--group dev`.

```bash
# Add a dev-only dependency
uv add --group dev pytest-cov

# Add multiple at once
uv add --group dev "ruff>=0.14" "mypy>=1.18"
```

### Optional Extras

Optional feature dependencies that aren't needed for the base install. These use the `[project.optional-dependencies]` section.

```bash
# Add an optional extra dependency
uv add --optional cloud stripe

# Install with an extra
uv sync --extra cloud
```

Currently defined extras: `cloud` (stripe).

## Installing Dependencies

### For Local Development

```bash
# Install base + dev dependencies
uv sync --group dev
```

### For Production

```bash
# Install base dependencies only (no dev tools)
uv sync
```

### For Docker Builds

The Dockerfile runs `uv sync --group dev --frozen` which installs from the locked versions without updating the lock file.

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

## Working with validibot-shared

The `validibot-shared` package is published to PyPI and installed as a normal dependency. When `validibot-shared` changes:

1. Make changes in `../validibot-shared`
2. Bump the version and publish to PyPI
3. In this project, run: `uv lock --upgrade-package validibot-shared && uv sync`

## Common Workflows

### Starting a New Feature

```bash
# Pull latest, sync dependencies
git pull
uv sync --group dev

# Load environment variables
source set-env.sh

# Run tests to verify setup
uv run pytest
```

### Adding a New Library

```bash
# Add the dependency
uv add requests

# Commit pyproject.toml and uv.lock
```

### Updating After a Merge

```bash
# If pyproject.toml or uv.lock changed
uv sync --group dev
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

# Validator Build Isolation

This document explains how validators are kept separate from the Django container despite being in the same git repository.

## Problem

The `validators/` directory is in the git repository alongside the Django code. When building the Django Docker container, we need to ensure validator code is **NOT** copied into the Django image.

## Solution: .dockerignore

The `.dockerignore` file at the repository root excludes `validators/` from Django builds:

```dockerignore
# Exclude validator containers - they build separately
validators/
```

This means:
- ✅ `validators/` is tracked in git (can commit, push, pull)
- ✅ `validators/` is excluded from Django Docker builds
- ✅ Django container stays small and focused
- ✅ Validators build independently with their own Dockerfiles

## How Validators Build

Each validator has its own Dockerfile and builds separately:

```bash
# Build EnergyPlus validator (from repo root)
docker build -t gcr.io/PROJECT/validibot-validator-energyplus \
  -f validators/energyplus/Dockerfile \
  validators/energyplus

# Build Django (from repo root)
docker build -t gcr.io/PROJECT/validibot-django \
  -f compose/production/django/Dockerfile \
  .
```

Notice:
- **Django build context:** `.` (entire repo, but `validators/` excluded by .dockerignore)
- **Validator build context:** `validators/energyplus` (only validator directory)

## Build Context Differences

### Django Container Build

```dockerfile
# compose/production/django/Dockerfile
WORKDIR /app
COPY pyproject.toml uv.lock /app/
RUN uv sync --no-dev --no-sources
COPY . /app/  # <-- This copies everything EXCEPT validators/ (due to .dockerignore)
```

**Includes:**
- Django code (`simplevalidations/`, `config/`, etc.)
- Documentation (`docs/`, `mkdocs.yml`)
- Configuration files
- **NOT validators/** (excluded by .dockerignore)
- **NOT site/** (excluded by .dockerignore)

### Validator Container Build

```dockerfile
# validators/energyplus/Dockerfile
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY ../shared /app/validators/shared  # Shared utilities
COPY . /app/validators/energyplus      # Only this validator
```

**Includes:**
- Only the specific validator (`validators/energyplus/`)
- Shared validator utilities (`validators/shared/`)
- **NOT Django code**
- **NOT other validators**

## Verification

To verify validators are excluded from Django builds:

```bash
# Build Django container and check size
docker build -t django-test -f compose/production/django/Dockerfile .
docker images django-test

# List files in container (should NOT include validators/)
docker run --rm django-test ls -la /app | grep validators || echo "No validators (correct!)"
```

## Benefits of This Approach

1. **Single Repository** - All code in one place, easy to version together
2. **Atomic Changes** - Update envelope schemas + validators + Django in one commit
3. **Build Isolation** - Each container only includes what it needs
4. **Size Optimization** - Django container doesn't include EnergyPlus dependencies
5. **Security** - Validator container doesn't include Django credentials/config

## File Size Impact

Without `.dockerignore` exclusion:
- Django image: ~800MB (includes unnecessary validator deps)

With `.dockerignore` exclusion:
- Django image: ~400MB (only Django dependencies)
- EnergyPlus validator: ~1.2GB (includes EnergyPlus binary)
- Each container is optimized for its purpose

## Related Files

- `.dockerignore` - Excludes validators from Django builds
- `.gitignore` - Excludes build artifacts (site/, .venv/, etc.)
- `validators/*/Dockerfile` - Individual validator Dockerfiles
- `compose/production/django/Dockerfile` - Django Dockerfile

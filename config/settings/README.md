# Django Settings

This directory contains Django settings modules for different environments.

## Structure

There are exactly four settings files:

| File | Purpose |
|------|---------|
| `base.py` | Shared settings inherited by all environments |
| `local.py` | Local development (DEBUG=True, console email, etc.) |
| `production.py` | All production deployments (GCP, AWS, Docker Compose) |
| `test.py` | Test runner configuration |

## Local Development

The recommended way to run Validibot locally is with Docker Compose:

```bash
docker compose up
```

This uses `DJANGO_SETTINGS_MODULE=config.settings.local` with `USE_DOCKER=yes`.

All services (Django, Postgres, Redis, Celery) run in Docker containers.

## Production Settings

The `production.py` file handles **all** production deployment targets. Platform-specific
configuration is controlled via the `DEPLOYMENT_TARGET` environment variable:

| `DEPLOYMENT_TARGET` | Infrastructure |
|---------------------|----------------|
| `docker_compose` | Docker Compose production Docker Compose (Celery, Docker socket, local/S3/GCS storage) |
| `gcp` | Google Cloud Platform (Cloud Run, Cloud Tasks, GCS) |
| `aws` | Amazon Web Services (future: ECS/Batch, SQS, S3) |

The settings file reads `DEPLOYMENT_TARGET` and branches accordingly to configure:

- Storage backends (local filesystem, GCS, or S3)
- Validator runner (Docker socket, Cloud Run Jobs, or AWS Batch)
- Task queue (Celery for docker_compose, Cloud Tasks for GCP)
- Platform-specific integrations

## Environment Files

Platform-specific values live in environment files, not in separate settings modules.

**Templates** are in `.envs.example/` (committed to git):

```
.envs.example/
├── .local/
│   ├── .django
│   └── .postgres
└── .production/
    ├── .docker-compose/
    │   ├── .django
    │   └── .postgres
    ├── .google-cloud/
    │   └── .django
    └── .aws/
        └── .django
```

**Your actual secrets** go in `.envs/` (gitignored):

```
.envs/
├── .local/
│   ├── .django
│   └── .postgres
└── .production/
    ├── .docker-compose/
    │   ├── .django          # DEPLOYMENT_TARGET=docker_compose
    │   └── .postgres
    ├── .google-cloud/
    │   └── .django          # DEPLOYMENT_TARGET=gcp
    └── .aws/
        └── .django          # DEPLOYMENT_TARGET=aws
```

Copy templates to `.envs/` and edit with your values. See `.envs.example/README.md` for setup instructions.

Each production env file must set:

```bash
DJANGO_SETTINGS_MODULE=config.settings.production
DEPLOYMENT_TARGET=docker_compose  # or gcp, aws
```

## Why This Structure?

1. **Single source of truth** — Production logic is in one file, making it easier to maintain
2. **Clear separation** — Environment differences are in env files, not scattered across settings modules
3. **Explicit configuration** — `DEPLOYMENT_TARGET` makes the deployment type visible and explicit
4. **Reduced duplication** — Common production settings (security, logging, etc.) aren't duplicated
5. **Secrets never committed** — `.envs/` is fully gitignored; templates live in `.envs.example/`

## Adding a New Deployment Target

1. Add the target name to `VALID_DEPLOYMENT_TARGETS` in `production.py`
2. Add conditional blocks for storage, validator runner, and other platform-specific settings
3. Create a template env file in `.envs.example/.production/.new-target/.django`

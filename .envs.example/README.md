# Environment Configuration Templates

This directory contains example environment files for different deployment scenarios.
Copy these to `.envs/` and edit with your actual values.

> ⚠️ **Security Warning**: The `.envs/` folder is gitignored and must NEVER be committed to version control, especially public repositories. It contains passwords, API keys, and other sensitive credentials. Only `.envs.example/` (this folder) should be committed.

## Quick Start

### Local Development

All services run in Docker containers. This is the simplest setup - no local
Postgres or Redis installation required.

```bash
# Create the .envs directory structure
mkdir -p .envs/.local

# Copy the templates
cp .envs.example/.local/.django .envs/.local/.django
cp .envs.example/.local/.postgres .envs/.local/.postgres

# Copy the build/recipe config. Recommended for every local stack:
# it drives recipe-level knobs like ENABLE_MCP_SERVER (Pro stacks)
# and build-time Pro/Enterprise packaging (community Docker builds).
cp .envs.example/.local/.build .envs/.local/.build

# For the MCP-enabled stacks (local-pro / local-cloud), also copy the
# MCP env file — the `mcp` container reads it. Plain `just local` skips it.
cp .envs.example/.local/.mcp .envs/.local/.mcp

# Edit the files and replace !!!SET...!!! placeholders with your values.
# For local-pro / local-cloud, flip ENABLE_MCP_SERVER=true in .build.
# For local-pro / local-cloud, generate one local MCP service key and put that
# paired secret in .local/.mcp and .local/.django. x402 is a cloud-only feature;
# if you exercise it (local-cloud only), its config lives in .local/.django —
# the cloud Django service is the only consumer now that the MCP server no
# longer handles payments.
# Then start the local stack:
just local up
```

**What runs where:**

- Django: Docker container (port 8000)
- Postgres: Docker container (port 5432)
- Redis: Docker container (port 6379)
- Celery worker: Docker container

### Self-Hosted (single-VM Docker Compose deployment)

The customer-operated production target — runs on a single Linux VM
(DigitalOcean, AWS EC2, Hetzner, on-prem). See
`docs/operations/self-hosting/overview.md` and ADR-2026-04-27.

```bash
# Create the directory structure
mkdir -p .envs/.production/.self-hosted

# Copy runtime files
cp .envs.example/.production/.self-hosted/.django .envs/.production/.self-hosted/.django
cp .envs.example/.production/.self-hosted/.postgres .envs/.production/.self-hosted/.postgres

# Copy the build/recipe config. Required if you set a commercial package
# (VALIDIBOT_COMMERCIAL_PACKAGE) or want the MCP container
# (ENABLE_MCP_SERVER=true). Safe to copy even if both stay unset.
cp .envs.example/.production/.self-hosted/.build .envs/.production/.self-hosted/.build

# For the MCP container (ENABLE_MCP_SERVER=true), also copy the MCP env file:
cp .envs.example/.production/.self-hosted/.mcp .envs/.production/.self-hosted/.mcp

# Edit with your production values (especially secrets!)
# Then validate and bootstrap with:
just self-hosted check-env
just self-hosted bootstrap
```

### Google Cloud Platform (Cloud Run)

```bash
# Create the directory structure
mkdir -p .envs/.production/.google-cloud

# Copy the template files
cp .envs.example/.production/.google-cloud/.django .envs/.production/.google-cloud/.django
cp .envs.example/.production/.google-cloud/.just .envs/.production/.google-cloud/.just
cp .envs.example/.production/.google-cloud/.build .envs/.production/.google-cloud/.build
cp .envs.example/.production/.google-cloud/.mcp .envs/.production/.google-cloud/.mcp

# Edit .django with your GCP project values (uploaded to Secret Manager)
# Edit .just with your GCP project ID and region (used locally by just commands)
# Edit .build with deploy-time knobs like ENABLE_MCP_SERVER and public MCP URLs
# (x402 payment config lives in .django, not .build)
# Edit .mcp with MCP-only secrets before uploading mcp-env

# Source the just config before running deployment commands
source .envs/.production/.google-cloud/.just
just gcp deploy-all prod          # build + push + migrate + web/worker/scheduler/mcp

# Secrets upload via the `secrets` recipes (run after editing .django / .mcp):
#   just gcp secrets prod          # umbrella: uploads BOTH .django and .mcp
#   just gcp django secrets prod   # only .django  (django-env)
#   just gcp mcp secrets prod      # only .mcp     (mcp-env)
```

**GCP config files:**

- `.django` - Django runtime settings, uploaded to Secret Manager
- `.just` - Host-side GCP command context (project ID, region), sourced locally
- `.build` - Build/deploy knobs read by just recipes; the shared non-secret MCP URLs are stamped into Cloud Run env vars (x402 config lives in `.django`)
- `.mcp` - MCP server secrets, uploaded to the separate `mcp-env` Secret Manager secret

### AWS (Future)

```bash
# Create the directory structure
mkdir -p .envs/.production/.aws

# Copy the template
cp .envs.example/.production/.aws/.django .envs/.production/.aws/.django

# Edit with your AWS values
# Note: AWS deployment is planned but not yet implemented
```

## Directory Structure

```
.envs.example/              # Templates (committed to git)
├── README.md
├── .local/
│   ├── .django             # Django settings for local dev
│   ├── .build              # Optional Docker build settings for Pro/Enterprise
│   ├── .mcp                # MCP server config (local-pro / local-cloud only)
│   └── .postgres           # Postgres credentials for local dev
└── .production/
    ├── .self-hosted/       # Customer-operated single-VM Compose deployment
    │   ├── .build          # Optional Docker build settings for Pro/Enterprise
    │   ├── .django
    │   ├── .mcp            # Optional MCP server config (Pro feature)
    │   └── .postgres
    ├── .google-cloud/      # Validibot's hosted GCP deployment
    │   ├── .django         # Django runtime settings (uploaded to Secret Manager)
    │   ├── .just           # Just command runner settings (sourced locally)
    │   ├── .build          # Deploy-time knobs stamped into Cloud Run env vars
    │   └── .mcp            # MCP server secrets (uploaded to mcp-env)
    └── .aws/               # Future AWS deployment (stub)
        └── .django

.envs/                      # Your actual secrets (NOT committed - gitignored)
├── .local/
│   ├── .django
│   ├── .build
│   └── .postgres
└── .production/
    ├── .self-hosted/
    │   ├── .build
    │   ├── .django
    │   ├── .mcp
    │   └── .postgres
    ├── .google-cloud/
    │   ├── .django
    │   └── .just
    └── .aws/
        └── .django
```

## Environment Variable Reference

### PostgreSQL Variables (`.postgres`)

| Variable            | Description       | Default                          |
| ------------------- | ----------------- | -------------------------------- |
| `POSTGRES_HOST`     | Database hostname | `postgres` (Docker service name) |
| `POSTGRES_PORT`     | Database port     | `5432`                           |
| `POSTGRES_DB`       | Database name     | `validibot`                      |
| `POSTGRES_USER`     | Database user     | -                                |
| `POSTGRES_PASSWORD` | Database password | -                                |

**Note:** `DATABASE_URL` is automatically constructed by the entrypoint script from these variables.

### Docker Build + Recipe Variables (`.build`)

The `.build` file plays two roles — both loaded from the same file:

1. **Docker build-time vars** — passed to `docker compose --env-file` for
   YAML interpolation of `${FOO}` references in the compose files
   (primarily build args that bake commercial packages into the image).
2. **Recipe-level knobs** — the `just local up` / `just local-pro up` /
   `just local-cloud up` recipes (and the production `just gcp` recipes)
   source this file at the top so shell-level variables like
   `ENABLE_MCP_SERVER` drive which Compose profiles get activated, and so the
   GCP deploy recipe can stamp a few public values (e.g. the MCP public URLs)
   onto Cloud Run via `--set-env-vars`.

`.build` is no longer mounted into any running container via `env_file`. Runtime
payment config (x402) moved to `.django` when the MCP server stopped handling
payments. All `.build` values are optional — if the file is absent the recipes
no-op cleanly where the stack does not need it.

| Variable | Role | Description | Example |
| --- | --- | --- | --- |
| `VALIDIBOT_COMMERCIAL_PACKAGE` | Build-time | **Self-hosted Pro/Enterprise operators:** the licensed package (or wheel URL) to bake into your Docker image, fetched from `VALIDIBOT_PRIVATE_INDEX_URL`. Installing it only makes the code _importable_ — to actually activate it you must _also_ set `DJANGO_SETTINGS_MODULE=config.settings.production_pro` in `.django`, which adds `validibot_pro` to `INSTALLED_APPS`. | `validibot-pro==0.1.0` |
| `VALIDIBOT_PRIVATE_INDEX_URL` | Build-time | Private package index URL from your license email. | `https://user:pass@pypi.validibot.com/simple/` |
| `ENABLE_MCP_SERVER` | Recipe | Activate the `mcp` Compose profile so the FastMCP container is built and started alongside the stack. Set to `true` for `just local-pro up` / `just local-cloud up`; ignored by `just local up` (community compose has no mcp service). | `true` / `false` |
| `VALIDIBOT_MCP_API_BASE_URL` | Recipe | GCP-only API URL stamped into MCP as `VALIDIBOT_API_BASE_URL` and Django web as `MCP_OIDC_AUDIENCE`. Required when `ENABLE_MCP_SERVER=true` on GCP. | `https://app.your-domain.example` |
| `VALIDIBOT_MCP_BASE_URL` | Recipe/runtime | GCP-only public MCP URL stamped into both Django and MCP as `VALIDIBOT_MCP_BASE_URL`, so OAuth audience/redirect metadata comes from one value. | `https://mcp.your-domain.example` |

> **Per-family note:** `VALIDIBOT_MCP_API_BASE_URL` and `VALIDIBOT_MCP_BASE_URL`
> are GCP-only and stay in `.production/.google-cloud/.build`; the deploy recipe
> stamps them onto Cloud Run via `--set-env-vars`. **x402 payment config is not
> here** — it lives in `.django` (read only by `validibot_cloud.settings`),
> because the cloud Django service is its sole consumer now that the MCP server
> no longer handles payments. See the X402 sections of the `.local/.django` and
> `.production/.google-cloud/.django` files.

### Django Variables (`.django`)

#### Core Settings

| Variable                 | Description                                          | Default                 | Required        |
| ------------------------ | ---------------------------------------------------- | ----------------------- | --------------- |
| `DJANGO_SETTINGS_MODULE` | Settings module path                                 | `config.settings.local` | Yes             |
| `DJANGO_SECRET_KEY`      | Secret key for cryptographic signing                 | -                       | Production only |
| `DJANGO_DEBUG`           | Enable debug mode                                    | `True` (local)          | No              |
| `DJANGO_ALLOWED_HOSTS`   | Comma-separated list of allowed hosts                | `*` (local)             | Production only |
| `DJANGO_ADMIN_URL`       | Admin URL path (randomize for production!)           | `admin/`                | No              |
| `DEPLOYMENT_TARGET`      | Deployment platform (`docker_compose`, `gcp`, `aws`) | -                       | Production only |

#### Security

| Variable                       | Description                                                                                                                          | Default          | Required        |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ | ---------------- | --------------- |
| `DJANGO_MFA_ENCRYPTION_KEY`    | Fernet key encrypting TOTP secrets + recovery-code seeds at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Never reuse across environments. | -                | Yes             |
| `MFA_TOTP_ISSUER`              | Label shown in users' authenticator apps next to their email (e.g. "Validibot Cloud").                                               | `Validibot`      | No              |
| `DJANGO_ADMIN_FORCE_ALLAUTH`   | Routes `/admin/login/` through allauth so admin inherits MFA enforcement, rate limiting, and session rotation. Flip to `False` only as a break-glass when allauth itself is broken (redeploy required). See [`docs/dev_docs/how-to/configure-mfa.md`](../docs/dev_docs/how-to/configure-mfa.md). | `False`          | No (but recommended `True` in production) |

#### Infrastructure

| Variable     | Description                 | Default                |
| ------------ | --------------------------- | ---------------------- |
| `USE_DOCKER` | Running in Docker container | `yes`                  |
| `REDIS_URL`  | Redis connection URL        | `redis://redis:6379/0` |

#### Email (Optional)

| Variable                | Description        |
| ----------------------- | ------------------ |
| `POSTMARK_SERVER_TOKEN` | Postmark API token |
| `MAILGUN_API_KEY`       | Mailgun API key    |
| `SENDGRID_API_KEY`      | SendGrid API key   |

If no email provider is configured, emails are printed to the console.

#### Feature Toggles

| Variable                            | Description            | Default |
| ----------------------------------- | ---------------------- | ------- |
| `DJANGO_ACCOUNT_ALLOW_REGISTRATION` | Allow new user signups | `true`  |
| `DJANGO_ACCOUNT_ALLOW_LOGIN`        | Allow user login       | `true`  |

#### Superuser (Initial Setup)

| Variable             | Description        | Default             |
| -------------------- | ------------------ | ------------------- |
| `SUPERUSER_USERNAME` | Admin username     | `admin`             |
| `SUPERUSER_PASSWORD` | Admin password     | -                   |
| `SUPERUSER_EMAIL`    | Admin email        | `admin@example.com` |
| `SUPERUSER_NAME`     | Admin display name | `Admin`             |

#### Celery (Optional)

| Variable                 | Description        | Default |
| ------------------------ | ------------------ | ------- |
| `CELERY_FLOWER_USER`     | Flower UI username | `debug` |
| `CELERY_FLOWER_PASSWORD` | Flower UI password | `debug` |

#### Production Security

| Variable                     | Description               | Default |
| ---------------------------- | ------------------------- | ------- |
| `DJANGO_SECURE_SSL_REDIRECT` | Redirect HTTP to HTTPS    | `true`  |
| `SENTRY_DSN`                 | Sentry error tracking DSN | -       |
| `WEB_CONCURRENCY`            | Gunicorn worker count     | `4`     |

#### GCP-Specific (Google Cloud)

| Variable                      | Description                        |
| ----------------------------- | ---------------------------------- |
| `GCP_PROJECT_ID`              | Google Cloud project ID            |
| `GCP_REGION`                  | Google Cloud region                |
| `CLOUD_SQL_CONNECTION_NAME`   | Cloud SQL instance connection name |
| `STORAGE_BUCKET`              | GCS bucket for file storage        |
| `GCS_TASK_QUEUE_NAME`         | Cloud Tasks queue name             |
| `CLOUD_TASKS_SERVICE_ACCOUNT` | Service account for Cloud Tasks    |

## Important Notes

1. **NEVER commit `.envs/` to version control** - This folder contains your real secrets and is gitignored. Committing it to a public repository would expose passwords, API keys, and other sensitive credentials.
2. **Generate real secrets** - Use the commands below to generate `DJANGO_SECRET_KEY` and passwords for production
3. **Platform-specific settings** - Each template includes only settings relevant to that deployment target
4. **Placeholder values** - Replace all `!!!SET...!!!` placeholders with actual values before running
5. **DATABASE_URL** - Automatically constructed by entrypoint; don't set manually for Docker Compose deployments
6. **Use different secrets per environment** - Dev, staging, and production should have completely different credentials

## Generating Secrets

### Django Secret Key

```bash
python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
```

### Admin URL Path

Randomize the admin URL to prevent automated attacks on `/admin/`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(16))"
```

Then set it in your env file (remember to add the trailing slash):

```
DJANGO_ADMIN_URL=k8Xm2pQ1wZ9nR4tB/
```

### Secure Password

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

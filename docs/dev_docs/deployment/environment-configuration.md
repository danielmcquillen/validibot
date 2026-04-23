# Environment Configuration

Validibot uses environment files to configure Django settings, database credentials, and deployment-specific options. This page explains the structure and usage of these files.

## Directory Structure

Environment configuration uses a template-based approach:

```
.envs.example/              # Templates (committed to git)
├── README.md               # Quick start guide and variable reference
├── .local/
│   ├── .django             # Django settings for local development
│   ├── .build              # Optional Docker build settings for Pro/Enterprise
│   └── .postgres           # Postgres credentials for local development
└── .production/
    ├── .docker-compose/    # Docker Compose production deployment
    │   ├── .build          # Optional Docker build settings for Pro/Enterprise
    │   ├── .django
    │   └── .postgres
    ├── .google-cloud/      # Google Cloud Platform deployment
    │   ├── .django         # Django runtime settings (uploaded to Secret Manager)
    │   └── .just           # Just command runner settings (sourced locally)
    └── .aws/               # AWS deployment (future)
        └── .django

.envs/                      # Your actual secrets (NOT committed - gitignored)
└── (same structure as above)
```

## Why This Structure?

The separation between `.envs.example/` and `.envs/` serves two purposes:

1. **Templates stay in version control**: The `.envs.example/` folder contains example configurations with placeholder values. These are committed to git so new developers can see what variables are needed.

2. **Secrets stay private**: The `.envs/` folder contains your actual credentials and is gitignored. **NEVER commit this folder to version control, especially public repositories** - it contains passwords, API keys, and other sensitive data that could compromise your deployment if exposed.

This pattern follows the [cookiecutter-django](https://github.com/cookiecutter/cookiecutter-django) convention, which is widely used in the Django community.

## Setup Workflow

### Local Development

1. Create the directory structure:

    ```bash
    mkdir -p .envs/.local
    ```

2. Copy the templates:

    ```bash
    cp .envs.example/.local/.django .envs/.local/.django
    cp .envs.example/.local/.postgres .envs/.local/.postgres
    # Optional for Pro/Enterprise
    cp .envs.example/.local/.build .envs/.local/.build
    ```

3. Edit the files to set real values (especially `SUPERUSER_PASSWORD`).

4. Start Docker Compose:

    ```bash
    just local up
    ```

### Docker Compose Production

1. Create the directory structure:

    ```bash
    mkdir -p .envs/.production/.docker-compose
    ```

2. Copy both template files:

    ```bash
    cp .envs.example/.production/.docker-compose/.django .envs/.production/.docker-compose/.django
    cp .envs.example/.production/.docker-compose/.postgres .envs/.production/.docker-compose/.postgres
    # Optional for Pro/Enterprise
    cp .envs.example/.production/.docker-compose/.build .envs/.production/.docker-compose/.build
    ```

3. Edit with your production values (generate a proper secret key, set your domain, etc.).

4. Validate the env files and bootstrap the deployment:

    ```bash
    just docker-compose check-env
    just docker-compose bootstrap
    ```

### Google Cloud Platform

GCP deployments don't use local `.envs/` files. Instead, secrets are stored in Google Secret Manager and mounted as environment files at runtime.

Start with [Deploy to GCP](deploy-gcp.md) for the high-level path, then use [Google Cloud Deployment](../google_cloud/deployment.md) for the full Cloud Run runbook.

## How Environment Files Work

### The Runtime Two-File Pattern

Each deployment environment uses two files:

| File | Purpose | Example Variables |
|------|---------|-------------------|
| `.postgres` | Database credentials only | `POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_PASSWORD` |
| `.django` | Everything else | `DJANGO_SECRET_KEY`, `REDIS_URL`, `SITE_URL` |

This separation keeps database credentials isolated and makes it clear which variables configure which service.

### The `.build` file — Docker builds AND recipe knobs

The `.build` file plays two roles, both loaded from the same file:

1. **Docker build-time vars.** Passed to `docker compose --env-file` for
   YAML interpolation of `${FOO}` references in the compose files. This
   is where you bake a commercial package into the image:

    - `VALIDIBOT_COMMERCIAL_PACKAGE` — must be an exact version like
      `validibot-pro==0.1.0` or a quoted exact wheel URL on
      `pypi.validibot.com` that includes `#sha256=<hash>`
    - `VALIDIBOT_PRIVATE_INDEX_URL`

2. **Recipe-level knobs.** The `just local up` / `just local-pro up` /
   `just local-cloud up` recipes (and the production
   `just docker-compose` recipes) source this file at the top, so
   shell-level variables drive recipe logic **before** `docker compose`
   is invoked. The canonical example is `ENABLE_MCP_SERVER`, which
   decides whether to activate the `mcp` Compose profile.

    - `ENABLE_MCP_SERVER=true` — include the FastMCP container in the
      stack. Flip to `true` for `local-pro` and `local-cloud`, where
      validibot-pro is installed and satisfies the runtime license
      gate. Ignored by `just local up` because the community compose
      file defines no `mcp` service.

Both categories are optional — if `.build` is absent, the recipes
no-op cleanly. For community Docker Compose self-hosters, the file
is effectively always worth copying because category (2) is how you
turn on MCP.

Pro / Enterprise reminder: installing the wheel via category (1)
gets the package into the image, but Django still needs the app in
`INSTALLED_APPS`. Use `config.settings.local_pro` /
`config.settings.production_pro` settings modules for that.

### DATABASE_URL Construction

You'll notice that `DATABASE_URL` is not in the environment files. This is intentional - the entrypoint script automatically constructs it from the individual postgres variables:

```bash
# From compose/local/django/entrypoint.sh
export DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
```

This means `.postgres` is the single source of truth for database credentials. You never need to duplicate connection details.

### Docker Compose Loading

Docker Compose loads both files via the `env_file` directive:

```yaml
services:
  django:
    env_file:
      - ./.envs/.local/.django
      - ./.envs/.local/.postgres
```

The order matters only if you have duplicate variables (later files override earlier ones).

## Platform-Specific Configuration

### Local Development (Docker Compose)

The local configuration is designed for simplicity:

- Uses Docker service names for hostnames (`postgres`, `redis`)
- Simple default credentials (fine for local dev)
- Console email backend (emails print to terminal)
- Debug mode enabled

Key files:
- `.envs/.local/.django` - Django configuration
- `.envs/.local/.postgres` - Postgres credentials

### Docker Compose Production

Docker Compose deployments run with production-grade settings:

- `DJANGO_SETTINGS_MODULE=config.settings.production`
- `DEPLOYMENT_TARGET=docker_compose`
- Real SSL certificates (via reverse proxy)
- Proper secret key and passwords

The `DEPLOYMENT_TARGET=docker_compose` setting tells Django to:
- Use Celery for background tasks (not Cloud Tasks)
- Use Docker socket for running validator containers
- Use local filesystem or S3/GCS for file storage

### Google Cloud Platform

GCP deployments use completely different infrastructure:

- `DEPLOYMENT_TARGET=gcp`
- Cloud SQL for database (via Unix socket)
- Cloud Tasks for background work
- Cloud Run Jobs for validator containers
- GCS for file storage (media + submissions)

Infrastructure is provisioned idempotently via
`just gcp init-stage {dev|staging|prod}`. After it finishes, the
recipe prints the env var values (e.g. `STORAGE_BUCKET`) to paste
into `.envs/.production/.google-cloud/.django` before the first
`just gcp secrets` upload. Commercial add-ons (e.g. the GCS
audit-archive backend) provision their own resources via their own
recipes on top of this baseline.

**Two types of environment files**:

| File | Purpose | Usage |
|------|---------|-------|
| `.django` | Django runtime settings | Uploaded to Secret Manager, mounted at `/secrets/.env` |
| `.just` | Just command runner settings | Sourced locally before running `just gcp` commands |

The `.just` file contains your GCP project ID and region, which the justfile needs to run deployment commands. Source it before running any `just gcp` commands:

```bash
# Source your GCP config
source .envs/.production/.google-cloud/.just

# Now you can run GCP commands
just gcp deploy prod
```

The `.django` file contains Django settings and is uploaded to Secret Manager:

```bash
just gcp secrets dev   # Upload secrets for dev environment
just gcp secrets prod  # Upload secrets for production
```

### AWS (Future)

AWS deployment support is planned but not yet implemented. The configuration will use:

- `DEPLOYMENT_TARGET=aws`
- AWS Batch for validator containers
- SQS for task queue
- S3 for file storage

## Environment Variable Reference

For a complete list of environment variables and their descriptions, see:

- `.envs.example/README.md` in the project root - Quick reference table with all variables
- [Configuration Settings](../overview/settings.md) - Django-specific settings documentation

## Internal API Security (Worker Endpoints)

Validibot's worker service exposes internal API endpoints for validation
execution, callbacks, and scheduled tasks. These endpoints need protection
against unauthorized access. The authentication backend is selected
automatically based on `DEPLOYMENT_TARGET` — see
[ADR-2026-04-18](https://github.com/mcquilleninteractive/validibot-project/blob/main/docs/adr/completed/2026-04-18-worker-endpoint-auth-platform-agnostic.md)
for the full design.

| Deployment target | Primary auth | Application-layer auth |
|---|---|---|
| **Docker Compose** | Network isolation | `WORKER_API_KEY` (shared secret) — **required** |
| **GCP** | Cloud Run IAM + private ingress | `CloudTasksOIDCAuthentication` — **required** |
| **AWS** *(not yet implemented)* | — | `WORKER_API_KEY` fallback |
| **Local dev / test** | — | None (skipped when key unset) |

Worker views inherit from `WorkerOnlyAPIView`, whose
`get_authenticators()` delegates to the deployment-aware factory in
`validibot/core/api/task_auth.py::get_worker_auth_classes()`. Adding a
new deployment target means writing one DRF `BaseAuthentication`
subclass and extending the factory — no view code changes.

### Shared-secret (`WORKER_API_KEY`) — Docker Compose

When the shared-secret backend is active, all requests to worker
endpoints must include the key in the `Authorization` header:

```
Authorization: Worker-Key <key>
```

When `WORKER_API_KEY` is empty (the default outside production), the
check is skipped. This allows local dev to run without provisioning a
key.

For Docker Compose deployments, add `WORKER_API_KEY` to
`.envs/.production/.docker-compose/.django`:

```bash
# Generate a key
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Add to .django env file
WORKER_API_KEY=your-generated-key-here
```

Since all Docker Compose services (web, worker, scheduler) share the
same env file, the key is automatically available to every service.
The Celery worker uses it when making internal API calls.

**Why this matters:** in Docker Compose, all containers share the
Docker bridge network. Without `WORKER_API_KEY`, an SSRF vulnerability
in the web container could let an attacker call worker endpoints
directly, potentially spoofing validation results or triggering data
deletion.

### OIDC identity tokens — GCP

On `DEPLOYMENT_TARGET=gcp`, worker endpoints verify a Google-signed
OIDC identity token on every request. The token is provided as
`Authorization: Bearer <jwt>` by the caller (Cloud Tasks, Cloud
Scheduler, or a validator Cloud Run Job via the metadata server).

Two settings control verification:

| Setting | What it does |
|---|---|
| `TASK_OIDC_AUDIENCE` | Expected `aud` claim. Must equal the worker service URL origin (scheme + host, **no path**). Falls back to `WORKER_URL` when unset. |
| `TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS` | Comma-separated list of service-account emails authorised to sign tokens. **Empty list → reject everything.** Legacy fallback: `CLOUD_TASKS_SERVICE_ACCOUNT`. |

Both settings live in the Secret Manager-mounted `.env` on the worker
service. A typical `.envs/.production/.google-cloud/.django`:

```bash
DEPLOYMENT_TARGET=gcp
WORKER_URL=https://validibot-worker-xxxx.run.app
# Optional — defaults to WORKER_URL
TASK_OIDC_AUDIENCE=https://validibot-worker-xxxx.run.app
TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS=validibot-cloudrun-prod@PROJECT.iam.gserviceaccount.com
```

**Set both explicitly in production.** The fallbacks (`WORKER_URL` for
audience, `CLOUD_TASKS_SERVICE_ACCOUNT` for the allowlist) are there so
the settings module doesn't raise `ImproperlyConfigured` on a minimal
`.env`, but leaving them implicit couples worker authentication to
settings whose primary purpose is something else. Rotating the Cloud
Tasks dispatcher SA, changing `WORKER_URL` to add a custom domain, or
splitting Cloud Scheduler onto its own SA all become accidentally
breaking changes if the auth layer is piggy-backing on them. Setting
`TASK_OIDC_AUDIENCE` and `TASK_OIDC_ALLOWED_SERVICE_ACCOUNTS` directly
makes the auth contract self-documenting and lets you rotate the two
concerns independently.

**Boot-time validation.** `config/settings/production.py` raises
`ImproperlyConfigured` at Cloud Run startup when `DEPLOYMENT_TARGET=gcp`
and the resolved audience or allowlist (after applying fallbacks) is
empty. Misconfiguration surfaces in the deploy log, not in production
traffic.

**Audience contract.** Cloud Tasks and Cloud Scheduler sign tokens
with `aud = <service URL origin>` — path and query are NOT included.
Django's strict verification enforces exact match. Validator
containers derive the audience the same way (see
`validibot-validators/validators/core/callback_auth.py`). If you front
the worker behind a load balancer with a different signed audience,
override with `TASK_OIDC_AUDIENCE`.

**Failure modes.** Missing header, missing signature, audience
mismatch, un-allowlisted signer, or unverified email all return 401
with an audit log line identifying the presented audience and signing
account. These are diagnosable from worker logs alone.

## Security Reminders

!!! danger "Never Commit Secrets"
    The `.envs/` folder must **NEVER** be committed to version control, especially public repositories. This folder is gitignored for a reason - it contains passwords, API keys, database credentials, and other sensitive data. Committing these files could expose your entire deployment to attackers.

1. **Generate proper secrets for production** - Use the commands in `.envs.example/README.md` to generate `DJANGO_SECRET_KEY` and passwords.

2. **Randomize the admin URL** - Change `DJANGO_ADMIN_URL` from the default `admin/` to a random path. This prevents automated scanners from finding your admin login page. Generate one with: `python -c "import secrets; print(secrets.token_urlsafe(16))"`.

3. **Use different secrets per environment** - Dev, staging, and production should have completely different credentials.

4. **Rotate secrets periodically** - Especially after team member departures or security incidents.

5. **Use Secret Manager in cloud deployments** - GCP Secret Manager (or AWS Secrets Manager) provides audit logging and access controls that local files can't.

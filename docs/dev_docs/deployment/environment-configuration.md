# Environment Configuration

Validibot uses environment files to configure Django settings, database credentials, and deployment-specific options. This page explains the structure and usage of these files.

## Directory Structure

Environment configuration uses a template-based approach:

```
.envs.example/              # Templates (committed to git)
├── README.md               # Quick start guide and variable reference
├── .local/
│   ├── .django             # Django settings for local development
│   └── .postgres           # Postgres credentials for local development
└── .production/
    ├── .docker-compose/    # Docker Compose production deployment
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
    ```

3. Edit the files to set real values (especially `SUPERUSER_PASSWORD`).

4. Start Docker Compose:

    ```bash
    docker compose up
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
    ```

3. Edit with your production values (generate a proper secret key, set your domain, etc.).

4. Deploy:

    ```bash
    docker compose -f docker-compose.production.yml up -d
    ```

### Google Cloud Platform

GCP deployments don't use local `.envs/` files. Instead, secrets are stored in Google Secret Manager and mounted as environment files at runtime.

See [Google Cloud Deployment](../google_cloud/deployment.md) for details.

## How Environment Files Work

### The Two-File Pattern

Each deployment environment uses two files:

| File | Purpose | Example Variables |
|------|---------|-------------------|
| `.postgres` | Database credentials only | `POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_PASSWORD` |
| `.django` | Everything else | `DJANGO_SECRET_KEY`, `REDIS_URL`, `SITE_URL` |

This separation keeps database credentials isolated and makes it clear which variables configure which service.

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
- GCS for file storage

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

## Internal API Security (WORKER_API_KEY)

Validibot's worker service exposes internal API endpoints for validation execution, callbacks, and scheduled tasks. These endpoints need protection against unauthorized access.

The `WORKER_API_KEY` setting provides a shared-secret authentication layer:

| Deployment | Primary Auth | WORKER_API_KEY |
|---|---|---|
| **Docker Compose** | None (same network) | **Required** - protects against SSRF |
| **GCP** | Cloud Run IAM (OIDC) | Optional - defense in depth |
| **Local dev** | None | Optional |

### How It Works

When `WORKER_API_KEY` is set, all requests to worker endpoints must include it in the Authorization header:

```
Authorization: Worker-Key <key>
```

When `WORKER_API_KEY` is empty (the default), the check is skipped. This allows GCP deployments to rely solely on Cloud Run IAM.

### Docker Compose Setup

For Docker Compose deployments, add `WORKER_API_KEY` to your `.envs/.production/.docker-compose/.django` file:

```bash
# Generate a key
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Add to .django env file
WORKER_API_KEY=your-generated-key-here
```

Since all Docker Compose services (web, worker, scheduler) share the same env file, the key is automatically available to all services. The Celery worker uses it when making internal API calls.

### Why This Matters

In Docker Compose, all containers share the same Docker bridge network. Without `WORKER_API_KEY`, an SSRF vulnerability in the web container could allow an attacker to call worker endpoints directly, potentially spoofing validation results or triggering data deletion.

## Security Reminders

!!! danger "Never Commit Secrets"
    The `.envs/` folder must **NEVER** be committed to version control, especially public repositories. This folder is gitignored for a reason - it contains passwords, API keys, database credentials, and other sensitive data. Committing these files could expose your entire deployment to attackers.

1. **Generate proper secrets for production** - Use the commands in `.envs.example/README.md` to generate `DJANGO_SECRET_KEY` and passwords.

2. **Use different secrets per environment** - Dev, staging, and production should have completely different credentials.

3. **Rotate secrets periodically** - Especially after team member departures or security incidents.

4. **Use Secret Manager in cloud deployments** - GCP Secret Manager (or AWS Secrets Manager) provides audit logging and access controls that local files can't.

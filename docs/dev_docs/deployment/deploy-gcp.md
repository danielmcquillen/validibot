# Deploy to GCP

Choose this target when you want a managed cloud deployment on Google Cloud instead of a self-managed single host.

This page is the high-level entry point for GCP deployments. For the deeper Cloud Run runbook, see [Google Cloud Deployment](../google_cloud/deployment.md).

## When to choose this target

Choose GCP if you want:

- managed application hosting on Cloud Run
- managed PostgreSQL with Cloud SQL
- Secret Manager, Artifact Registry, and Cloud Scheduler integration
- a cleaner fit for teams already standardised on Google Cloud

Choose [Deploy with Docker Compose](deploy-docker-compose.md) instead if you want the simplest self-hosted production path on infrastructure you control directly.

## What this target runs

The GCP deployment uses:

- Cloud Run for the web service
- Cloud Run for the worker service
- Cloud SQL for PostgreSQL
- Cloud Storage for file storage
- Secret Manager for runtime configuration
- Artifact Registry for container images
- Cloud Scheduler for recurring jobs

Advanced validators are deployed separately from the main web and worker services.

## Environment model

The GCP setup is designed around three stages:

| Stage | Purpose | Typical use |
| --- | --- | --- |
| `dev` | development testing | deploy new changes first |
| `staging` | pre-production verification | optional but useful for larger changes |
| `prod` | production | customer-facing environment |

Each stage gets its own Cloud Run services, Cloud SQL instance, secrets, and queueing resources.

## Signed credentials on GCP

GCP deployments should use Google Cloud KMS rather than a local PEM file.
Set the credential-signing key in your stage `.django` env file:

```bash
GCP_KMS_SIGNING_KEY=projects/your-project/locations/your-region/keyRings/your-app-name-keys/cryptoKeys/credential-signing
CREDENTIAL_ISSUER_URL=https://validibot.example.com
```

The Cloud Run service account also needs permission to sign with that key.
At minimum, grant the runtime service account:

- `roles/cloudkms.viewer`
- `roles/cloudkms.signerVerifier`

Use a different KMS key per stage so dev, staging, and prod credentials do not
share the same issuer key material.

## Typical first-time flow

Most first-time GCP setups follow this order:

```bash
source .envs/.production/.google-cloud/.just

just gcp init-stage dev
just gcp secrets dev
just gcp deploy-all dev
just gcp migrate dev
just gcp setup-data dev
just gcp validators-deploy-all dev
just gcp scheduler-setup dev
```

After that, verify the environment, then repeat the same process for `staging` or `prod` as needed.

### Secrets checklist

Before `just gcp secrets dev`, make sure `.envs/.production/.google-cloud/.django`
defines:

- `DJANGO_SECRET_KEY` — Django session / signed-cookie key.
- `DJANGO_MFA_ENCRYPTION_KEY` — Fernet key for MFA secret material. The
  app refuses to start without this, and the startup check validates
  the format (not just presence). Generate with:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `DATABASE_URL`, `POSTGRES_*` — Cloud SQL connection.
- `MFA_TOTP_ISSUER` — authenticator-app label (e.g. "Validibot Cloud").
- `STORAGE_BUCKET` — media / submission bucket, printed at the end of
  `init-stage`.

Commercial add-ons may introduce additional env vars (for example, a
GCS audit-archive bucket with CMEK encryption). Each add-on's own
deployment docs lists the env vars it expects — a community GCP
deployment uses the null / filesystem audit-archive backends and
needs nothing beyond the list above.

### Provisioned resources

`just gcp init-stage {stage}` is idempotent and creates, among other
things:

- Runtime and validator service accounts with IAM bindings.
- Cloud SQL instance and database.
- Cloud Tasks queue and Cloud Scheduler-ready KMS permissions.
- Media/submissions GCS bucket (`{app}-storage[-stage]`) with
  public/private prefix IAM.
- Secret Manager placeholder for `django-env[-stage]`.

A community-only deployment uses the ``NullArchiveBackend`` for audit
log retention, which needs no extra GCP resources. Deployments that
layer on a commercial add-on with the GCS audit-archive backend provision
the bucket, CMEK key, and IAM separately — see the add-on's own
deployment docs.

See [configure-mfa.md](../how-to/configure-mfa.md) for key-generation
and rotation procedures. The encryption key is stored in Secret Manager
via `just gcp secrets`, never committed.

### Cache table

Production uses Django's `DatabaseCache` backend by default (rather
than Memorystore/Redis) — a zero-marginal-cost option that reuses
the Cloud SQL instance for allauth rate limiting and TOTP replay
protection. The `just gcp migrate` step runs `createcachetable`
automatically on every deploy (idempotent — no-op after the first
run). If you ever need higher cache throughput, set `REDIS_URL` to a
Memorystore instance and the settings module switches backends
automatically — see
[configure-mfa.md](../how-to/configure-mfa.md#upgrade-path-redis-via-memorystore)
for the full upgrade path.

## Routine deployment flow

For normal updates:

```bash
source .envs/.production/.google-cloud/.just

just gcp deploy-all dev
just gcp migrate dev
```

Promote to production only after the lower stage looks healthy.

## Include the MCP server

The standalone FastMCP container exposes validation workflows to AI
agents over the Model Context Protocol. On GCP it runs as its own
Cloud Run service (`validibot-mcp` in prod, `validibot-mcp-<stage>`
otherwise) with its own Artifact Registry image and service account,
deployed independently from the main Django web service.

**Source and image.** The MCP code lives in this repo at `mcp/` and
is built from `compose/production/mcp/Dockerfile`. The image is a
lightweight Python container (~80 MB) with FastMCP, httpx, and
pydantic-settings only — no Django, no database drivers.

**License gate.** At startup the MCP server calls
`GET /api/v1/license/features/` against the Django API and refuses
to serve traffic unless `mcp_server` is advertised. This only
happens when `validibot-pro` (or enterprise) is installed. So a
community-only deployment can build and deploy the image but the
container will exit during the license check.

### Configure the knobs

The MCP deploy tooling reads two values from
`.envs/.production/.google-cloud/.build`:

```bash
# Include the MCP container in ``just gcp deploy-all`` and unlock
# the ``just gcp mcp ...`` recipes. Requires validibot-pro to be
# installed so the runtime license check passes.
ENABLE_MCP_SERVER=true

# Public URL of YOUR Validibot Django API — the MCP server proxies
# tool calls here. There is no default; setting this wrong could
# accidentally proxy your users' traffic to another operator's API.
VALIDIBOT_MCP_API_BASE_URL=https://app.your-domain.example
```

See `.envs.example/.production/.google-cloud/.build` for the full
documented template.

### Deploy

First-time setup provisions the MCP service account, IAM bindings,
and Artifact Registry access:

```bash
source .envs/.production/.google-cloud/.just
just gcp mcp setup prod
```

Then upload the MCP secret (OAuth client credentials, etc.) and
deploy the service. You have three levels of granularity:

```bash
# Umbrella — pushes every secret that might have changed
just gcp secrets prod
# Equivalent to: gcp django secrets + gcp mcp secrets

# Surgical — just one service
just gcp django secrets prod   # only .django → django-env
just gcp mcp secrets prod      # only .mcp → mcp-env
```

```bash
# Full deploy — Django web + worker + scheduler + MCP build + MCP deploy
just gcp deploy-all prod

# MCP-only deploy — useful for hotfixing just the MCP image
just gcp mcp build
just gcp mcp deploy prod
```

### Routing

To expose MCP on a custom domain via the load balancer you set up
for Django, run:

```bash
just gcp mcp lb-add prod mcp.your-domain.example
```

That provisions a serverless NEG, a backend service, adds the MCP
hostname to the managed SSL certificate, and locks the Cloud Run
service's ingress to load-balancer-only.

## Domain and networking

There are two normal ways to expose a GCP deployment publicly:

- Cloud Run domain mappings for the simpler path in supported regions
- a global HTTP(S) load balancer for the more production-oriented path

If you need a custom domain, SSL, or a single public entrypoint, see the domain section in [Google Cloud Deployment](../google_cloud/deployment.md).

## Good fits for this target

GCP is a good fit when:

- you already use Google Cloud
- you want managed infrastructure rather than running a VM yourself
- you need a cleaner path to multi-environment deployments

## Read next

Use these guides after choosing GCP:

- [Google Cloud Deployment](../google_cloud/deployment.md)
- [Google Cloud Overview](../google_cloud/index.md)
- [Google Cloud Setup Cheatsheet](../google_cloud/setup-cheatsheet.md)
- [Google Cloud Scheduled Jobs](../google_cloud/scheduled-jobs.md)

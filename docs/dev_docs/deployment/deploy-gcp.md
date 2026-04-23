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
- `AUDIT_ARCHIVE_GCS_BUCKET` and `AUDIT_ARCHIVE_GCS_PROJECT_ID` — the
  CMEK-encrypted audit archive bucket (also printed at the end of
  `init-stage`). A Django startup check refuses to run `migrate` if
  `AUDIT_ARCHIVE_GCS_BUCKET` is empty while the cloud settings module
  is active, so these must be set before the first deploy.

### Provisioned resources

`just gcp init-stage {stage}` is idempotent and creates, among other
things:

- Runtime and validator service accounts with IAM bindings.
- Cloud SQL instance and database.
- Cloud Tasks queue and Cloud Scheduler-ready KMS permissions.
- Media/submissions GCS bucket (`{app}-storage[-stage]`) with
  public/private prefix IAM.
- **Audit archive GCS bucket** (`{project}-{app}-audit-archive[-stage]`)
  with CMEK encryption, lifecycle tiering (Nearline/Coldline/Archive),
  and append-only IAM. Provisioned by the `audit-archive-setup` recipe,
  also runnable standalone to catch up an older stage. See the
  [cloud audit-archive operations guide](../../../../validibot-cloud/docs/operations/audit-archive.md)
  for the full shape.
- Secret Manager placeholder for `django-env[-stage]`.

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

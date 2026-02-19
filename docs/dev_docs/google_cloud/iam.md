# IAM & Service Accounts

This document covers how Validibot uses Google Cloud IAM (Identity and Access Management) for secure access to GCP resources.

!!! note "Resource naming convention"
    All GCP resource names are derived from `GCP_APP_NAME`, which is set in
    `.envs/.production/.google-cloud/.just` (defaults to `validibot`). The
    naming pattern is `$GCP_APP_NAME-{resource}[-{stage}]`. For example, with
    the default app name, the dev service account is `validibot-cloudrun-dev`
    and the prod storage bucket is `validibot-storage`.

## Overview

We use two types of service accounts per environment:

- **Web/Worker SA** (`$GCP_APP_NAME-cloudrun-{stage}`) - Used by Cloud Run web and worker services. Has broad access to run the Django application.
- **Validator SA** (`$GCP_APP_NAME-validator-{stage}`) - Used by validator Cloud Run Jobs (EnergyPlus, FMI). Least-privilege: only storage access and worker callback permission.

This ensures:

- Environment isolation (dev can't access prod data)
- Least privilege (validators can't read secrets or access the database)
- No hardcoded credentials in code

## Service Accounts

### Web/Worker Service Account

| Stage | Service Account |
| ----- | --------------- |
| dev | `$GCP_APP_NAME-cloudrun-dev` |
| staging | `$GCP_APP_NAME-cloudrun-staging` |
| prod | `$GCP_APP_NAME-cloudrun-prod` |

**Roles granted:**

| Role | Scope | Purpose |
| ---- | ----- | ------- |
| `roles/cloudsql.client` | Project | Connect to Cloud SQL |
| `roles/secretmanager.secretAccessor` | Project | Read secrets |
| `roles/run.invoker` | Project | Invoke Cloud Run services/jobs |
| `roles/cloudtasks.enqueuer` | Project | Create tasks in queues |
| `roles/cloudtasks.viewer` | Project | View queue status |
| `roles/storage.objectAdmin` | Stage bucket | Read/write storage objects |
| `roles/cloudkms.viewer` | KMS key | View signing key metadata |
| `roles/cloudkms.signerVerifier` | KMS key | Sign validation credentials |
| `roles/iam.serviceAccountTokenCreator` | Self | Create OIDC tokens for Cloud Tasks |
| `roles/iam.serviceAccountUser` | Self | Act as the service account |
| Custom `validibot_job_runner` | Validator jobs | Trigger jobs with env overrides |

### Validator Service Account

| Stage | Service Account |
| ----- | --------------- |
| dev | `$GCP_APP_NAME-validator-dev` |
| staging | `$GCP_APP_NAME-validator-staging` |
| prod | `$GCP_APP_NAME-validator-prod` |

**Roles granted:**

| Role | Scope | Purpose |
| ---- | ----- | ------- |
| `roles/storage.objectAdmin` | Stage bucket | Read inputs, write outputs |
| `roles/run.invoker` | Worker service | POST callbacks with results |

The validator SA deliberately does **not** have:

- `secretmanager.secretAccessor` (no access to Django secrets, Stripe keys, etc.)
- `cloudsql.client` (no database access)
- `cloudtasks.enqueuer` (no task queue access)
- KMS roles (no credential signing)

This limits the blast radius if a validator container is compromised by a malicious user-provided model (IDF, FMU, etc.).

## Setup

All service accounts and IAM bindings are created automatically by `just gcp init-stage`:

```bash
just gcp init-stage dev      # Creates both SAs + all bindings
just gcp init-stage prod     # Same for production
```

The `just gcp validator-deploy` command additionally grants:

- `validibot_job_runner` on the job to the main SA (so web/worker can trigger it)
- `roles/run.invoker` on the worker service to the validator SA (so the job can POST callbacks)

## Application Default Credentials (ADC)

Our Django application uses [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials) to authenticate with GCP services. This means:

- **No credentials in code** - No JSON key files, no access keys
- **Automatic detection** - Libraries detect the environment and use appropriate credentials
- **Environment-specific** - Uses local user credentials for development, service account for Cloud Run

### How ADC Works

| Environment | Credential Source |
| ----------- | ----------------- |
| Local dev | `gcloud auth application-default login` |
| Cloud Run | Attached service account (metadata) |

### Local Development Setup

To use GCP services locally (optional - local filesystem works for most development):

```bash
# One-time setup - opens browser for OAuth
gcloud auth application-default login
```

This stores credentials at `~/.config/gcloud/application_default_credentials.json`.

The `django-storages` library and other Google Cloud libraries automatically detect and use these credentials.

## Security Best Practices

### Do

- Use separate service accounts per environment
- Use dedicated least-privilege SAs for untrusted workloads (validators)
- Grant permissions at the resource level (bucket, service) when possible
- Rely on ADC instead of key files

### Don't

- Use the same service account for dev and prod
- Grant broad roles to components that don't need them
- Create and download JSON key files unless absolutely necessary
- Store credentials in code or version control

## Troubleshooting

### "Could not automatically determine credentials"

ADC isn't configured. Solutions:

- **Locally**: Run `gcloud auth application-default login`
- **Cloud Run**: Check that a service account is attached to the service

### "Permission denied" errors

The service account doesn't have the required role. Check:

1. The service account is attached to the Cloud Run service/job
2. The service account has the correct role on the specific resource
3. The role is on the right resource (e.g., the correct bucket)

### Verifying Service Account Permissions

```bash
# List roles for the web/worker SA
gcloud projects get-iam-policy $GCP_PROJECT_ID \
    --flatten="bindings[].members" \
    --filter="bindings.members:$GCP_APP_NAME-cloudrun-prod" \
    --format="table(bindings.role)"

# List roles for the validator SA
gcloud projects get-iam-policy $GCP_PROJECT_ID \
    --flatten="bindings[].members" \
    --filter="bindings.members:$GCP_APP_NAME-validator-prod" \
    --format="table(bindings.role)"
```

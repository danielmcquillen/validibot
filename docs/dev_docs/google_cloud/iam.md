# IAM & Service Accounts

This document covers how Validibot uses Google Cloud IAM (Identity and Access Management) for secure access to GCP resources.

!!! note "Resource naming convention"
    All GCP resource names are derived from `GCP_APP_NAME`, which is set in
    `.envs/.production/.google-cloud/.just` (defaults to `validibot`). The
    naming pattern is `$GCP_APP_NAME-{resource}[-{stage}]`. For example, with
    the default app name, the dev service account is `validibot-cloudrun-dev`
    and the prod storage bucket is `validibot-storage`.

## Overview

We use three service-account roles per environment:

- **Web/Worker SA** (`$GCP_APP_NAME-cloudrun-{stage}`) - Used by Cloud Run web and worker services. Has broad access to run the Django application.
- **Validator runtime SA** (`$GCP_APP_NAME-validator-{stage}`) - Used by validator Services and retained Jobs. It can invoke the worker for callbacks/capability renewal but has no ambient storage role.
- **Provider invoker SA** (`$GCP_APP_NAME-val-invoker-{stage}`) - Attached only to provider-queue tasks and the sole `run.invoker` member on validator Services. It has no project roles and is not a callback identity. The abbreviated name keeps every supported stage within Google's service-account ID length limit.

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
| Custom `validibot_job_runner` | Project | Read validator Job/Service configuration and Service IAM; trigger Jobs with env overrides |

### Validator Service Account

| Stage | Service Account |
| ----- | --------------- |
| dev | `$GCP_APP_NAME-validator-dev` |
| staging | `$GCP_APP_NAME-validator-staging` |
| prod | `$GCP_APP_NAME-validator-prod` |

**Roles granted:**

| Role | Scope | Purpose |
| ---- | ----- | ------- |
| `roles/run.invoker` | Worker service | POST callbacks with results |

The validator SA deliberately does **not** have:

- `secretmanager.secretAccessor` (no access to Django secrets, Stripe keys, etc.)
- `cloudsql.client` (no database access)
- `cloudtasks.enqueuer` (no task queue access)
- KMS roles (no credential signing)
- any project, bucket, or managed-folder storage role

Django issues a short-lived Credential Access Boundary token for one attempt.
The token exposes only the `roles/storage.objectViewer` and
`roles/storage.objectCreator` permission ceilings below that attempt prefix;
because it has no delete permission, it cannot replace an existing object.
This prevents a compromised validator processing a malicious IDF, FMU, RDF, or
XML document from reading another attempt through its metadata identity.

### MCP Service Account

Only relevant on deployments that run the MCP server. Provisioned by
`just gcp mcp setup <stage>` when `ENABLE_MCP_SERVER=true` is set in
`.envs/<stage>/.google-cloud/.build`.

| Stage | Service Account |
| ----- | --------------- |
| dev | `$GCP_APP_NAME-mcp-dev` |
| staging | `$GCP_APP_NAME-mcp-staging` |
| prod | `$GCP_APP_NAME-mcp-prod` |

**Roles granted:**

| Role | Scope | Purpose |
| ---- | ----- | ------- |
| `roles/secretmanager.secretAccessor` | Exact stage `mcp-env` secret | Read only the MCP OAuth/runtime environment; setup removes the legacy project-wide binding |
| `roles/run.invoker` | Django web service | Mint OIDC identity tokens to call `/api/v1/mcp/*` on Django |

The MCP SA deliberately does **not** have:

- `cloudsql.client` (no database access — MCP talks to Django over REST)
- `cloudtasks.enqueuer` (no task queue access)
- `roles/storage.objectAdmin` (no storage access)
- KMS roles (no credential signing)

`just gcp security-audit <stage>` verifies that MCP has no project-level role,
has no user-managed key, and has exactly one `secretAccessor` binding on its
stage `mcp-env` secret.

This is the most constrained SA in the deployment. Even a full MCP
container compromise only exposes the OAuth client secret and lets
the attacker call Django's `/api/v1/mcp/*` surface — everything
interesting (workflows, runs, submissions) is still gated by the
end user's forwarded identity.

For the identity-token flow to work, Django must also be configured
to accept tokens minted by this SA. Set
`MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS` in `.envs/<stage>/.google-cloud/.django`
to include the email. The deploy recipe stamps `MCP_OIDC_AUDIENCE` onto
Django from `VALIDIBOT_MCP_API_BASE_URL` in `.build`; do not set it separately.
See [Deploy to GCP — Configure MCP auth](../deployment/deploy-gcp.md)
for the full setting list.

## Setup

The web/worker and validator service accounts are created automatically
by `just gcp init-stage`:

```bash
just gcp init-stage dev      # Creates web/worker + validator SAs + all bindings
just gcp init-stage prod     # Same for production
```

The MCP service account is created separately — only if you're
running MCP — by:

```bash
just gcp mcp setup dev       # Creates MCP SA + secret/run.invoker bindings
just gcp mcp setup prod
```

The Job half of `just gcp validator-deploy` (also available directly as
`just gcp validator-job-deploy`) additionally grants:

- `validibot_job_runner` on the job to the main SA (so web/worker can inspect
  and trigger it; `init-stage` also grants this custom role at project scope so
  deployment sync and drift checks can read validator Services and their IAM)
- `roles/run.invoker` on the worker service to the validator SA (so the job can POST callbacks)

The custom role keeps its historical ID but is intentionally narrower than
`roles/run.viewer`. Its permissions are `run.jobs.get`, `run.jobs.run`,
`run.jobs.runWithOverrides`, `run.services.get`, and
`run.services.getIamPolicy`. The read permissions let deployment registration
verify exact digests, ready revisions, resource settings, and the sole Service
invoker before changing a route. It cannot list unrelated resources or modify
Service configuration or IAM.

Every supported GCP validator image consumes the mandatory attempt token. Prove
the downscoped token's provider behavior with:

```bash
cd /Users/danielmcquillen/projects/validibot/validibot
just gcp validator-storage-capability-probe prod
```

The maintenance-safe acceptance command removes historical validator storage
bindings and evaluates the service account's effective object permissions with
[Policy Troubleshooter](https://cloud.google.com/policy-intelligence/docs/troubleshoot-access):

```bash
cd /Users/danielmcquillen/projects/validibot/validibot
just gcp validator-acceptance prod v0.15.1
```

That operation removes the known legacy bindings, rejects remaining direct
predefined `roles/storage*` bindings, and fails unless effective object
get/list/create/update/delete permissions are conclusively `CANNOT_ACCESS`.
Policy Troubleshooter evaluates inherited, group, primitive, custom-role, and
conditional policy paths; an unknown result is not proof and stops the recipe.
It also runs the real capability probe and representative validators. On
failure it restores the capability-aware Job route but does not restore any
ambient storage binding. IAM denial is a deployment invariant, not an
environment assertion.

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

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

# Google Cloud Integration

This section documents Validibot's integration with Google Cloud Platform (GCP). The platform runs on GCP with Australian data residency, using Cloud Run for compute and Cloud SQL for data storage.

## Quick Start

All deployment commands require a stage parameter (`dev`, `staging`, or `prod`):

```bash
just gcp-deploy dev       # Deploy web service to dev
just gcp-deploy-all dev   # Deploy web + worker to dev
just gcp-migrate dev      # Run database migrations on dev
just gcp-logs dev         # View recent logs for dev
just gcp-status dev       # Show dev service URL and status
```

Run `just` (with no arguments) to see all available commands.

## Architecture Overview

The GCP architecture includes:

- **Cloud Run (web)** - Django app serving user traffic
- **Cloud Run (worker)** - Background processing and validator callbacks
- **External HTTP(S) Load Balancer** - Serves `validibot.com` and routes to Cloud Run (serverless NEG)
- **Cloud SQL** - Managed PostgreSQL database
- **Cloud Storage** - Object storage for media files
- **Cloud Tasks** - Async task queue for validation jobs
- **Cloud Scheduler** - Cron jobs for maintenance tasks
- **Secret Manager** - Secure credential storage

## Documentation

- [Deployment Guide](deployment.md) - Deployments, operations, and custom domain setup
- [Logging](logging.md) - Cloud Logging setup and searching logs
- [Scheduled Jobs](scheduled-jobs.md) - Cloud Scheduler configuration
- [Security](security.md) - Cloud SQL networking, database access, secrets
- [Setup Cheatsheet](setup-cheatsheet.md) - Initial GCP setup steps and reference
- [Storage (GCS)](storage.md) - Cloud Storage configuration for media files
- [IAM & Service Accounts](iam.md) - Identity and access management setup

## Related

- [Validator Jobs (Cloud Run)](../validator_jobs_cloud_run.md) - Validator container architecture and multi-environment deployment
- [ADR: Google Cloud Platform Architecture](../../adr/2025-12-02-google-cloud-platform.md) - Platform architecture and implementation

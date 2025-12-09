# Google Cloud Integration

This section documents Validibot's integration with Google Cloud Platform (GCP). The platform runs on GCP with Australian data residency, using Cloud Run for compute and Cloud SQL for data storage.

## Quick Start

The `justfile` provides all common deployment operations:

```bash
just gcp-deploy      # Build, push, and deploy web service (routine updates)
just gcp-setup-all   # Full environment setup: web + worker + scheduler (first time)
just gcp-migrate     # Run database migrations
just gcp-logs        # View recent logs
just gcp-status      # Show service URL and status
```

Run `just` (with no arguments) to see all available commands.

## Architecture Overview

The GCP architecture includes:

- **Cloud Run (web)** - Django app serving user traffic
- **Cloud Run (worker)** - Background processing and validator callbacks
- **Cloud SQL** - Managed PostgreSQL database
- **Cloud Storage** - Object storage for media files
- **Cloud Tasks** - Async task queue for validation jobs
- **Cloud Scheduler** - Cron jobs for maintenance tasks
- **Secret Manager** - Secure credential storage

## Documentation

- [Deployment Guide](deployment.md) - How to deploy to Cloud Run
- [Logging](logging.md) - Cloud Logging setup and searching logs
- [Scheduled Jobs](scheduled-jobs.md) - Cloud Scheduler configuration
- [Setup Cheatsheet](setup-cheatsheet.md) - Initial GCP setup steps and reference
- [Storage (GCS)](storage.md) - Cloud Storage configuration for media files
- [IAM & Service Accounts](iam.md) - Identity and access management setup

## Related

- [ADR: Google Cloud Platform Architecture](../../adr/2025-12-02-google-cloud-platform.md) - Platform architecture and implementation

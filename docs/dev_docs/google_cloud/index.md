# Google Cloud Integration

This section documents Validibot's integration with Google Cloud Platform (GCP). We're migrating from Heroku to a GCP-native architecture to enable Australian data residency, remove Heroku's 30-second timeout constraint, and simplify our async processing model.

## Architecture Overview

Our target GCP architecture includes:

- **Cloud Run** - Serverless containers for web and worker services
- **Cloud SQL** - Managed PostgreSQL database
- **Cloud Storage** - Object storage for media files (replacing AWS S3)
- **Cloud Tasks** - Async task queue (replacing Celery + Redis)
- **Secret Manager** - Secure credential storage

## Documentation

- [Storage (GCS)](storage.md) - Cloud Storage configuration for media files
- [IAM & Service Accounts](iam.md) - Identity and access management setup

## Related

- [ADR: Heroku to GCP Migration](../../adr/2025-12-02-heroku-to-gcp-migration.md) - Complete migration plan and rationale

# ADR-2025-12-02: Google Cloud Platform Architecture

**Status:** Implemented
**Date:** 2025-12-02 (Updated: 2025-12-10)
**Owner:** Daniel / Validibot Platform
**Related ADRs:** [Phase 3 Cloud Run Design](../dev_docs/adr/2025-12-04-phase-3-django-cloud-run-design.md), [Validator Job Interface](2025-12-04-validator-job-interface.md)

---

## Summary

Validibot runs on Google Cloud Platform with Australian data residency (`australia-southeast1`). This ADR documents the platform architecture and implementation status.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    GOOGLE CLOUD (australia-southeast1)               │
│                                                                      │
│  ┌──────────────────┐         ┌──────────────────┐                  │
│  │   Cloud Run      │         │   Cloud Run      │                  │
│  │   "web"          │         │   "worker"       │                  │
│  │   (Public)       │         │   (IAM-protected)│                  │
│  │                  │         │                  │                  │
│  │  - Django app    │         │  - API endpoints │                  │
│  │  - Web UI        │         │  - Callbacks     │                  │
│  │  - APP_ROLE=web  │         │  - APP_ROLE=     │                  │
│  │                  │         │    worker        │                  │
│  └────────┬─────────┘         └────────┬─────────┘                  │
│           │                            ▲                             │
│           │      ┌────────────────┐    │ OIDC                        │
│           └──────┤  Cloud Tasks   ├────┘                             │
│                  │  (Job Queue)   │                                  │
│                  └────────────────┘                                  │
│                            ▲                                         │
│  ┌────────────────┐        │        ┌────────────────┐              │
│  │   Cloud SQL    │        │        │ Cloud Storage  │              │
│  │   PostgreSQL   │        │        │ (Media/Files)  │              │
│  └────────────────┘        │        └────────────────┘              │
│                            │                                         │
│  ┌────────────────┐  ┌─────┴────────┐  ┌────────────────┐           │
│  │ Cloud Scheduler│  │ Cloud Run    │  │ Secret Manager │           │
│  │ (Cron jobs)    │  │ Jobs         │  │ (Credentials)  │           │
│  └────────────────┘  │ (Validators) │  └────────────────┘           │
│                      └──────────────┘                               │
└─────────────────────────────────────────────────────────────────────┘

                              │
                              ▼
              ┌─────────────────┐    ┌─────────────────┐
              │   Postmark      │    │   Sentry        │
              │   (Email)       │    │  (Monitoring)   │
              └─────────────────┘    └─────────────────┘
```

### Services

| Service | Purpose | Access |
|---------|---------|--------|
| **Cloud Run (web)** | Django app, web UI, marketing pages | Public |
| **Cloud Run (worker)** | API endpoints, validator callbacks, scheduled tasks | IAM-protected |
| **Cloud Run Jobs** | Long-running validators (EnergyPlus, FMI) | Triggered by Cloud Tasks |
| **Cloud SQL** | PostgreSQL 17 database | Private (Cloud SQL Connector) |
| **Cloud Storage** | Media files, validation artifacts | Private + signed URLs |
| **Cloud Tasks** | Async job queue for validation runs | Internal |
| **Cloud Scheduler** | Cron jobs (cleanup, maintenance) | Internal |
| **Secret Manager** | Credentials and API keys | Service account access |

---

## Deployment

### Quick Start

```bash
# Full environment setup (first time)
just gcp-setup-all

# Regular code deployment
just gcp-deploy

# Run migrations
just gcp-migrate
```

See [Deployment Guide](../dev_docs/google_cloud/deployment.md) for details.

### Two-Service Model

The same Docker image runs as two Cloud Run services with different roles:

- **`APP_ROLE=web`**: Public-facing, serves web UI and marketing pages
- **`APP_ROLE=worker`**: IAM-protected, serves API and handles callbacks

This separation ensures:
- Public traffic cannot reach internal APIs
- Cloud Run IAM enforces authentication for worker endpoints
- Validators and Cloud Scheduler can only call the worker service

---

## Validation Execution Flow

1. **User submits validation** via web UI or API
2. **Worker creates** `ValidationRun` and enqueues Cloud Task
3. **Cloud Tasks** triggers Cloud Run Job (e.g., `validibot-validator-energyplus`)
4. **Validator Job** reads input from GCS, runs validation, writes results to GCS
5. **Job calls back** to worker service with results URI
6. **Worker processes** results and updates database

See [Validator Job Interface ADR](2025-12-04-validator-job-interface.md) for the contract specification.

---

## Scheduled Tasks

Cloud Scheduler replaces Celery Beat for periodic tasks:

| Job | Schedule | Purpose |
|-----|----------|---------|
| `validibot-clear-sessions` | Daily 2 AM | Clear expired Django sessions |
| `validibot-cleanup-idempotency-keys` | Daily 3 AM | Delete expired API keys |
| `validibot-cleanup-callback-receipts` | Weekly Sun 4 AM | Delete old callback receipts |

Setup: `just gcp-scheduler-setup`

See [Scheduled Jobs](../dev_docs/google_cloud/scheduled-jobs.md) for details.

---

## Security

### Authentication

- **Web service**: Django session auth, API keys for headless clients
- **Worker service**: Cloud Run IAM with OIDC tokens
- **Cloud Tasks → Worker**: Service account with `roles/run.invoker`
- **Cloud Scheduler → Worker**: Same OIDC pattern
- **Validator Jobs → Worker**: Service account ID tokens

### Secrets

All secrets in Secret Manager, mounted as `/secrets/.env`:

- `DJANGO_SECRET_KEY`
- `DATABASE_URL`
- `POSTMARK_SERVER_TOKEN`
- `SENTRY_DSN`

### Data Residency

All services run in `australia-southeast1` (Sydney):
- Compute: Cloud Run
- Database: Cloud SQL
- Storage: Cloud Storage
- Queues: Cloud Tasks

---

## Implementation Status

### Completed

- [x] GCP project setup and APIs enabled
- [x] Cloud Storage buckets (media, files)
- [x] Cloud SQL PostgreSQL instance
- [x] Secret Manager configuration
- [x] Cloud Run web service deployment
- [x] Cloud Run worker service deployment
- [x] Cloud Tasks queues
- [x] Cloud Scheduler jobs
- [x] Validator callback endpoint
- [x] Idempotency key cleanup
- [x] Session cleanup
- [x] Docker image and CI/CD
- [x] justfile deployment commands

### Remaining for Go-Live

- [ ] Custom domain mapping (`app.validibot.com`)
- [ ] DNS configuration
- [ ] TLS certificate (auto-managed by Cloud Run)
- [ ] Production data seeding
- [ ] Monitoring alerts in Cloud Monitoring

---

## Cost Estimates

| Service | Estimated Monthly Cost |
|---------|------------------------|
| Cloud Run (web + worker) | ~$20-50 |
| Cloud SQL (db-g1-small) | ~$25 |
| Cloud Storage | ~$5 |
| Cloud Tasks / Scheduler | ~$1 |
| **Total** | **~$50-80** |

Costs scale with usage. Cloud Run charges only for active requests.

---

## Related Documentation

- [Deployment Guide](../dev_docs/google_cloud/deployment.md)
- [Scheduled Jobs](../dev_docs/google_cloud/scheduled-jobs.md)
- [IAM & Service Accounts](../dev_docs/google_cloud/iam.md)
- [Storage Configuration](../dev_docs/google_cloud/storage.md)
- [Go-Live Checklist](../dev_docs/deployment/go-live-checklist.md)

# ADR-2025-12-02: Complete Migration from Heroku to Google Cloud Platform

**Status:** In Progress  
**Date:** 2025-12-02  
**Owner:** Daniel / Validibot Platform  
**Related ADRs:** 2025-12-01 Google Platform (superseded by this document), FMI Storage & Security Review

---

## Executive Summary

This ADR documents the complete migration of Validibot from Heroku to Google Cloud Platform (GCP). We are moving from a Heroku-hosted Django application with Celery workers and Redis to a GCP-native architecture using Cloud Run, Cloud Tasks, Cloud SQL, and Cloud Storage. This migration enables Australian data residency, removes the Heroku 30-second timeout constraint, simplifies our async processing model, and positions us for future multi-region deployment.

---

## Table of Contents

1. [Context and Motivation](#1-context-and-motivation)
2. [Current Architecture (Heroku)](#2-current-architecture-heroku)
3. [Target Architecture (Google Cloud)](#3-target-architecture-google-cloud)
4. [Work Already Completed](#4-work-already-completed)
5. [Detailed Migration Plan](#5-detailed-migration-plan)
6. [Service-by-Service Migration Guide](#6-service-by-service-migration-guide)
7. [Security Considerations](#7-security-considerations)
8. [Local Development Changes](#8-local-development-changes)
9. [Testing Strategy](#9-testing-strategy)
10. [Rollback Plan](#10-rollback-plan)
11. [Timeline and Milestones](#11-timeline-and-milestones)
12. [Open Questions](#12-open-questions)
13. [Glossary](#13-glossary)

---

## 1. Context and Motivation

### Why We Are Migrating

Validibot is a data validation orchestration platform that helps organizations build, manage, and execute validation workflows. Our current Heroku-based infrastructure has several limitations that impact our ability to serve customers effectively:

**1. Heroku's 30-Second Request Timeout**

Heroku's router enforces a hard 30-second timeout on all HTTP requests. This means even simple validations that take longer than 30 seconds must be processed asynchronously through Celery. This adds complexity:

- Every validation request must be queued to Celery
- We need Redis as a message broker
- We need separate worker dynos running Celery
- More moving parts means more things that can break

On Google Cloud Run, we can configure request timeouts up to 60 minutes, allowing many "basic" validations to run synchronously without the Celery overhead.

**2. No Australian Region on Heroku Common Runtime**

Our primary target market is Australian organizations with data sovereignty requirements. Heroku's Common Runtime only supports US and EU regions. While we can put media files in an Australian S3 bucket, the application itself and the database run overseas. This makes it difficult to offer a genuine "AU-first" data residency story.

Google Cloud has a Sydney region (`australia-southeast1`) where we can run our entire stack: compute, database, and storage.

**3. Celery and Redis Operational Overhead**

Running Celery adds significant operational complexity:

- Redis must be provisioned, monitored, and maintained
- Worker dynos must be scaled separately from web dynos
- Failed tasks need monitoring and retry configuration
- Celery Beat (for scheduled tasks) needs its own process

For a solo founder, this is a lot of infrastructure to manage. Cloud Tasks provides similar functionality with less operational burden.

**4. Cost Efficiency**

Heroku's pricing model charges for always-on dynos. Cloud Run charges only for actual compute time, which can be more cost-effective for variable workloads.

**5. Future Multi-Region Support**

We want to eventually offer regional deployments (AU, US, EU) for customers in different jurisdictions. Building on GCP from the start makes it easier to replicate our stack across regions later.

### What We Are NOT Changing

- **Modal.com for Heavy Compute**: We will continue using Modal.com for EnergyPlus simulations, FMU (Functional Mock-up Unit) execution, and other CPU-intensive validation work. Modal provides excellent isolation and performance for these workloads.
- **Core Django Application**: The Django application code, data models, and business logic remain largely unchanged. We're changing where and how it runs, not what it does.
- **API Contracts**: External API endpoints remain the same. Clients won't need to change their integrations.

---

## 2. Current Architecture (Heroku)

### System Components

```
┌─────────────────────────────────────────────────────────────────────┐
│                           HEROKU                                     │
│                                                                      │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐          │
│  │   Web Dyno   │    │ Worker Dyno  │    │  Beat Dyno   │          │
│  │   (Django    │    │   (Celery    │    │   (Celery    │          │
│  │   Gunicorn)  │    │   Workers)   │    │    Beat)     │          │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘          │
│         │                   │                   │                   │
│         └───────────────────┼───────────────────┘                   │
│                             │                                        │
│                    ┌────────┴────────┐                              │
│                    │      Redis      │                              │
│                    │  (Heroku Data)  │                              │
│                    └─────────────────┘                              │
│                                                                      │
│                    ┌─────────────────┐                              │
│                    │ Heroku Postgres │                              │
│                    │    (US/EU)      │                              │
│                    └─────────────────┘                              │
└─────────────────────────────────────────────────────────────────────┘

                              │
                              ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│    AWS S3       │    │   Modal.com     │    │   Postmark      │
│  (Media files)  │    │ (Heavy compute) │    │   (Email)       │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

### How Validation Currently Works

1. **Client Request**: A client submits content to validate via the API or web UI.

2. **Submission Creation**: The web dyno receives the request, creates a `Submission` record (the content to validate) and a `ValidationRun` record (tracking the execution).

3. **Celery Task Dispatch**: The web dyno enqueues a Celery task (`execute_validation_run`) to Redis. This happens even for quick validations because of the 30-second timeout constraint.

4. **Worker Execution**: A Celery worker picks up the task from Redis and executes the validation:

   - For **basic validators** (JSON Schema, XML Schema, CEL expressions): The worker runs the validation logic directly.
   - For **advanced validators** (EnergyPlus, FMI): The worker calls Modal.com to run the actual simulation, then processes the results.

5. **Result Storage**: Results are written to the database (`ValidationFinding`, `ValidationRunSummary` tables).

6. **Client Response**: The API returns immediately with a `202 Accepted` and a polling URL, or waits briefly for completion and returns `201 Created` with results.

### Current Celery Tasks

Looking at our codebase, we have these Celery tasks:

```python
# validibot/validations/tasks.py
@shared_task
def execute_validation_run(validation_run_id, user_id, metadata):
    """Main task that executes a validation run."""

@shared_task
def run_fmu_probe_task(fmu_model_id):
    """Probe an uploaded FMU to extract metadata."""
```

We also use `django_celery_beat` for scheduled tasks, though currently we don't have many recurring jobs defined.

### Pain Points

- **Complexity**: Three dyno types (web, worker, beat), Redis, plus external services
- **Cost**: Paying for idle worker dynos
- **Debugging**: Tracing issues across web → Redis → worker is harder than a single request
- **Timeout Workaround**: Everything async even when not needed

---

## 3. Target Architecture (Google Cloud)

### System Components

```
┌─────────────────────────────────────────────────────────────────────┐
│                    GOOGLE CLOUD (australia-southeast1)               │
│                                                                      │
│  ┌──────────────────┐         ┌──────────────────┐                  │
│  │   Cloud Run      │         │   Cloud Run      │                  │
│  │   "web"          │         │   "worker"       │                  │
│  │   (Public HTTP)  │         │   (Internal only)│                  │
│  │                  │         │                  │                  │
│  │  - Django app    │         │  - Django app    │                  │
│  │  - User requests │         │  - Task handlers │                  │
│  │  - API endpoints │         │  - /internal/*   │                  │
│  └────────┬─────────┘         └────────┬─────────┘                  │
│           │                            │                             │
│           │      ┌────────────────┐    │                             │
│           └──────┤  Cloud Tasks   ├────┘                             │
│                  │  (Task Queue)  │                                  │
│                  └────────────────┘                                  │
│                                                                      │
│  ┌────────────────┐    ┌────────────────┐    ┌────────────────┐     │
│  │   Cloud SQL    │    │ Cloud Storage  │    │ Secret Manager │     │
│  │   (Postgres)   │    │ (Media/Files)  │    │ (Credentials)  │     │
│  └────────────────┘    └────────────────┘    └────────────────┘     │
│                                                                      │
│  ┌────────────────┐    ┌────────────────┐                           │
│  │ Cloud Scheduler│    │ Cloud Jobs     │                           │
│  │ (Cron tasks)   │    │ (Long-running) │                           │
│  └────────────────┘    └────────────────┘                           │
└─────────────────────────────────────────────────────────────────────┘

                              │
                              ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Modal.com     │    │   Postmark      │    │   Sentry        │
│ (Heavy compute) │    │   (Email)       │    │  (Monitoring)   │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

### Component Explanations

**Cloud Run ("web" service)**

- Runs our Django application container
- Handles all public HTTP traffic (web UI, public API)
- Scales automatically based on request load
- Can scale to zero when idle (cost savings)
- Publicly accessible with authentication handled by Django

**Cloud Run ("worker" service)**

- Runs the same Django container, different entrypoint
- Handles internal task execution (validation runs, FMU probes)
- NOT publicly accessible - only reachable by Cloud Tasks
- Endpoints protected by Cloud Tasks OIDC authentication

**Cloud Tasks**

- Replaces Redis + Celery for async job dispatch
- Provides automatic retries with exponential backoff
- Guarantees at-least-once delivery
- Queue-based with configurable rate limits
- Built-in dead-letter handling for failed tasks

**Cloud SQL (PostgreSQL)**

- Managed PostgreSQL database
- Located in Australian region (data sovereignty)
- Automated backups and point-in-time recovery
- Connected to Cloud Run via Cloud SQL Connector

**Cloud Storage**

- Replaces AWS S3 for media files
- **Two-bucket strategy** for security and public access control:
  - **Media buckets** (public): `validibot-media` (prod), `validibot-media-dev` (dev)
    - Public read-only (`allUsers` have `objectViewer`)
    - Used for blog images, workflow featured images, user avatars
    - Direct public URLs (no signed URLs needed)
  - **Files buckets** (private): `validibot-files` (prod), `validibot-files-dev` (dev)
    - Private access only (Cloud Run service account has `objectAdmin`)
    - Used for submissions, FMU uploads, validation artifacts, user data
    - Private URLs (requires authentication)
- Both bucket types use uniform bucket-level access (no per-object ACLs)

**Secret Manager**

- Stores sensitive configuration (database passwords, API keys)
- Injected into Cloud Run as environment variables
- Versioned with automatic rotation support

**Cloud Scheduler**

- Replaces Celery Beat for scheduled tasks
- Triggers Cloud Tasks or HTTP endpoints on a cron schedule
- Examples: cleanup jobs, digest emails, metric aggregation

**Cloud Jobs**

- For very long-running operations (beyond Cloud Run's timeout)
- Used when Modal isn't appropriate (e.g., database maintenance)
- We may not need this initially

### How Validation Will Work (New Flow)

1. **Client Request**: Same as before - client submits via API or web UI.

2. **Submission Creation**: The "web" Cloud Run service receives the request, creates `Submission` and `ValidationRun` records.

3. **Task Dispatch**: Instead of Celery, we enqueue a Cloud Task:

   ```python
   from google.cloud import tasks_v2

   client = tasks_v2.CloudTasksClient()
   task = {
       "http_request": {
           "http_method": "POST",
           "url": "https://worker-xxx.run.app/internal/run-validation/123/",
           "oidc_token": {"service_account_email": "tasks@project.iam.gserviceaccount.com"},
           "body": json.dumps({"metadata": {...}}).encode(),
       }
   }
   client.create_task(parent=queue_path, task=task)
   ```

4. **Worker Execution**: Cloud Tasks calls our "worker" Cloud Run service. The Django view:

   - Validates the OIDC token (rejects unauthorized requests)
   - Loads the `ValidationRun`
   - Executes validators (basic inline, advanced via Modal)
   - Updates results in the database

5. **Client Response**: Same as before - immediate response with polling URL or sync wait.

### Key Differences from Celery

| Aspect         | Celery + Redis            | Cloud Tasks                                |
| -------------- | ------------------------- | ------------------------------------------ |
| Message Broker | Redis (managed by us)     | Cloud Tasks (managed by Google)            |
| Workers        | Separate dyno/process     | Same Cloud Run service, different endpoint |
| Serialization  | Python pickle or JSON     | HTTP request body (JSON)                   |
| Retries        | Configurable per-task     | Configurable per-queue                     |
| Monitoring     | Flower, custom dashboards | Cloud Console, Cloud Monitoring            |
| Dead Letter    | Manual configuration      | Built-in dead-letter queues                |
| Authentication | N/A (internal)            | OIDC tokens (cryptographically verified)   |

---

## 4. Work Already Completed

### Google Cloud Account Setup

- Created Google Cloud project
- Enabled billing
- Enabled required APIs (Cloud Run, Cloud SQL, Cloud Storage, Cloud Tasks, Secret Manager)

### Cloud Storage Buckets

- **`validibot-au-media`**: Production bucket in `australia-southeast1`
- **`validibot-au-media-dev`**: Development/staging bucket

Both buckets have:

- Object versioning enabled (protection against accidental deletion)
- Private ACL (no public access)
- Lifecycle rules for old versions

### Django Storage Configuration

Updated Django settings to use Cloud Storage via `django-storages`:

- **Production** (`config/settings/production.py`): Uses `GoogleCloudStorage` backend with ADC
- **Local** (`config/settings/local.py`): Defaults to filesystem, optional GCS via `GCS_MEDIA_BUCKET` env var
- **Dependencies**: Added `django-storages[google,s3]` to `pyproject.toml`
- No explicit credentials in code - relies on Application Default Credentials

See [Storage Documentation](../docs/dev_docs/google_cloud/storage.md) for details.

### IAM & Service Accounts

Created environment-specific service accounts:

- **`validibot-dev-app`**: Runtime identity for dev Cloud Run services
- **`validibot-prod-app`**: Runtime identity for prod Cloud Run services

Each service account has `Storage Object Admin` role on its environment's bucket only (bucket-level, not project-level).

See [IAM Documentation](../docs/dev_docs/google_cloud/iam.md) for details.

### Docker Configuration

We've containerized the application for local development:

**`compose/local/django/Dockerfile`**:

- Base image: `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`
- Uses `uv` for fast dependency management
- Includes `vb_shared` as local editable dependency

**`docker-compose.local.yml`**:

- `django` service: Main web server on port 8000
- `worker` service: Simulates separate worker on port 8001
- `postgres` service: Local PostgreSQL database
- `mailpit` service: Local email testing
- Named volume for shared `.venv` between containers

### Dependency Management

Migrated from `requirements.txt` files to `uv`-native `pyproject.toml`:

- All dependencies defined in `pyproject.toml`
- Lock file (`uv.lock`) ensures reproducible builds
- Legacy `requirements/*.txt` files regenerated for Heroku compatibility (until full migration)

### django-cloud-tasks Integration

Added `django-cloud-tasks` package:

- Registered in `INSTALLED_APPS`
- Will provide decorators for defining Cloud Tasks-callable functions

---

## 5. Detailed Migration Plan

### Phase 1: Production Settings for GCP (Current)

**Goal**: Configure Django settings to work with GCP services.

**Tasks**:

1. **Update `config/settings/production.py`**:

   ```python
   # Cloud Storage for media (already done)
   STORAGES = {
       "default": {
           "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
           "OPTIONS": {"bucket_name": GCP_MEDIA_BUCKET},
       },
       "staticfiles": {
           "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
       },
   }

   # Cloud SQL connection
   DATABASES = {
       "default": {
           "ENGINE": "django.db.backends.postgresql",
           "HOST": "/cloudsql/PROJECT:REGION:INSTANCE",
           "NAME": "validibot",
           "USER": env("DB_USER"),
           "PASSWORD": env("DB_PASSWORD"),
       }
   }

   # Cloud Tasks configuration
   DJANGO_CLOUD_TASKS = {
       "project_location_name": "projects/validibot/locations/australia-southeast1",
       "task_handler_root_url": "https://worker-xxx.run.app",
   }
   DJANGO_CLOUD_TASKS_HANDLER_SECRET = env("CLOUD_TASKS_SECRET")
   ```

2. **Add Cloud Tasks URL route**:

   ```python
   # config/urls.py
   if getattr(settings, 'DJANGO_CLOUD_TASKS', None):
       urlpatterns.append(
           path('_tasks/', include('django_cloud_tasks.urls')),
       )
   ```

3. **Create internal worker endpoints**:

   ```python
   # validibot/validations/views_internal.py
   from django.views.decorators.csrf import csrf_exempt
   from django.http import JsonResponse

   @csrf_exempt
   def run_validation_task(request, validation_run_id):
       """Cloud Tasks target for validation execution."""
       # Validate OIDC token
       # Execute validation
       # Return 200/500
   ```

### Phase 2: Cloud SQL Setup

**Goal**: Provision and configure managed PostgreSQL.

**Steps**:

1. **Create Cloud SQL Instance**:

   ```bash
   gcloud sql instances create validibot-prod \
     --database-version=POSTGRES_17 \
     --edition=ENTERPRISE \
     --tier=db-f1-micro \
     --region=australia-southeast1 \
     --storage-type=SSD \
     --storage-size=10GB \
     --backup \
     --backup-start-time=03:00
   ```

2. **Create Database and User**:

   ```bash
   gcloud sql databases create validibot --instance=validibot-prod
   gcloud sql users create validibot --instance=validibot-prod --password=xxx
   ```

3. **Store Credentials in Secret Manager**:

   ```bash
   echo -n "password123" | gcloud secrets create db-password --data-file=-
   ```

4. **Configure Cloud Run Connection**:
   Cloud Run connects to Cloud SQL via a Unix socket (no public IP needed). The connection string format is:
   ```
   /cloudsql/PROJECT_ID:REGION:INSTANCE_NAME
   ```

### Phase 3: Cloud Run Deployment

**Goal**: Deploy Django application to Cloud Run.

**Steps**:

1. **Create Production Dockerfile**:

   ```dockerfile
   FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

   ENV PYTHONDONTWRITEBYTECODE=1 \
       PYTHONUNBUFFERED=1 \
       UV_COMPILE_BYTECODE=1

   WORKDIR /app

   # Install system dependencies
   RUN apt-get update && apt-get install -y --no-install-recommends \
       libpq-dev gettext curl && rm -rf /var/lib/apt/lists/*

   # Install Python dependencies
   COPY pyproject.toml uv.lock ./
   RUN uv sync --frozen --no-dev

   # Copy application
   COPY . .

   # Collect static files
   RUN uv run python manage.py collectstatic --noinput

   # Run as non-root
   RUN useradd -m django && chown -R django:django /app
   USER django

   # Gunicorn entrypoint
   CMD ["uv", "run", "gunicorn", "config.wsgi:application", \
        "--bind", "0.0.0.0:8080", "--workers", "2"]
   ```

2. **Build and Push Image**:

   ```bash
   # Build
   gcloud builds submit --tag gcr.io/PROJECT/validibot:latest

   # Or use Cloud Build with a cloudbuild.yaml
   ```

3. **Deploy "web" Service**:

   ```bash
   gcloud run deploy validibot-web \
     --image gcr.io/PROJECT/validibot:latest \
     --region australia-southeast1 \
     --platform managed \
     --allow-unauthenticated \
     --add-cloudsql-instances PROJECT:australia-southeast1:validibot-prod \
     --set-env-vars DJANGO_SETTINGS_MODULE=config.settings.production \
     --set-secrets DB_PASSWORD=db-password:latest \
     --memory 512Mi \
     --timeout 300
   ```

4. **Deploy "worker" Service**:
   ```bash
   gcloud run deploy validibot-worker \
     --image gcr.io/PROJECT/validibot:latest \
     --region australia-southeast1 \
     --platform managed \
     --no-allow-unauthenticated \  # Internal only!
     --add-cloudsql-instances PROJECT:australia-southeast1:validibot-prod \
     --set-env-vars DJANGO_SETTINGS_MODULE=config.settings.production \
     --set-secrets DB_PASSWORD=db-password:latest \
     --memory 1Gi \
     --timeout 900 \  # 15 minutes for longer tasks
     --command "uv" \
     --args "run,gunicorn,config.wsgi:application,--bind,0.0.0.0:8080"
   ```

### Phase 4: Cloud Tasks Setup

**Goal**: Configure task queues and authentication.

**Steps**:

1. **Create Task Queues**:

   ```bash
   # Main validation queue
   gcloud tasks queues create validation-runs \
     --location australia-southeast1 \
     --max-dispatches-per-second 10 \
     --max-concurrent-dispatches 20 \
     --max-attempts 5 \
     --min-backoff 10s \
     --max-backoff 300s

   # FMU probe queue (lower priority)
   gcloud tasks queues create fmu-probes \
     --location australia-southeast1 \
     --max-dispatches-per-second 2 \
     --max-concurrent-dispatches 5
   ```

2. **Create Service Account for Tasks**:

   ```bash
   gcloud iam service-accounts create cloud-tasks-invoker \
     --display-name "Cloud Tasks Invoker"

   # Grant permission to invoke worker service
   gcloud run services add-iam-policy-binding validibot-worker \
     --member serviceAccount:cloud-tasks-invoker@PROJECT.iam.gserviceaccount.com \
     --role roles/run.invoker \
     --region australia-southeast1
   ```

3. **Update Django to Enqueue Tasks**:

   ```python
   # validibot/validations/services/task_queue.py
   from google.cloud import tasks_v2
   from django.conf import settings
   import json

   def enqueue_validation_run(validation_run_id: int, metadata: dict = None):
       """Enqueue a validation run to Cloud Tasks."""
       client = tasks_v2.CloudTasksClient()

       project = settings.GCP_PROJECT_ID
       location = settings.GCP_LOCATION
       queue = "validation-runs"

       parent = client.queue_path(project, location, queue)

       worker_url = settings.CLOUD_TASKS_WORKER_URL
       url = f"{worker_url}/internal/run-validation/{validation_run_id}/"

       task = {
           "http_request": {
               "http_method": tasks_v2.HttpMethod.POST,
               "url": url,
               "headers": {
                   "Content-Type": "application/json",
                   "X-DCT-SECRET": settings.DJANGO_CLOUD_TASKS_HANDLER_SECRET,
               },
               "body": json.dumps({"metadata": metadata or {}}).encode(),
               "oidc_token": {
                   "service_account_email": f"cloud-tasks-invoker@{project}.iam.gserviceaccount.com",
                   "audience": worker_url,
               },
           },
       }

       client.create_task(parent=parent, task=task)
   ```

### Phase 5: Migrate Celery Tasks

**Goal**: Replace Celery task calls with Cloud Tasks enqueuing.

**Changes Required**:

1. **`ValidationRunService.launch()`**:

   Current code:

   ```python
   async_result = execute_validation_run.apply_async(
       kwargs={
           "validation_run_id": validation_run.id,
           "user_id": request.user.id,
           "metadata": metadata or {},
       },
       countdown=2,
   )
   ```

   New code:

   ```python
   from validibot.validations.services.task_queue import enqueue_validation_run

   transaction.on_commit(lambda: enqueue_validation_run(
       validation_run_id=validation_run.id,
       metadata={"user_id": request.user.id, **(metadata or {})},
   ))
   ```

2. **FMU Probe Task**:
   Similar change - create `enqueue_fmu_probe()` function.

3. **Worker Endpoint Views**:

   ```python
   # validibot/validations/views_internal.py

   @csrf_exempt
   def run_validation(request, validation_run_id):
       """Cloud Tasks target for validation execution."""

       # Verify authentication
       if not verify_cloud_tasks_request(request):
           return JsonResponse({"error": "Unauthorized"}, status=403)

       # Execute validation
       service = ValidationRunService()
       try:
           result = service.execute(
               validation_run_id=validation_run_id,
               user_id=request.POST.get("user_id"),
               metadata=json.loads(request.body).get("metadata", {}),
           )
           return JsonResponse({"status": "ok"}, status=200)
       except Exception as e:
           logger.exception("Validation failed")
           return JsonResponse({"error": str(e)}, status=500)
   ```

### Phase 6: Cloud Scheduler (Celery Beat Replacement)

**Goal**: Set up scheduled tasks.

Currently, we use `django_celery_beat` but don't have many scheduled tasks defined. When we add them:

```bash
# Example: Daily cleanup job at 3 AM Sydney time
gcloud scheduler jobs create http cleanup-old-runs \
  --schedule "0 3 * * *" \
  --time-zone "Australia/Sydney" \
  --uri "https://validibot-worker.run.app/internal/cleanup/" \
  --oidc-service-account-email cloud-tasks-invoker@PROJECT.iam.gserviceaccount.com \
  --location australia-southeast1
```

### Phase 7: Advanced Validators (Modal Integration)

**Goal**: Ensure Modal.com validators work from GCP.

Modal.com is used for:

- **EnergyPlus simulations**: Building energy modeling
- **FMI (Functional Mock-up Unit) execution**: Co-simulation
- **FMU probing**: Metadata extraction

These run on Modal's infrastructure (AWS-based) and communicate with our Django app via:

1. Our Django worker calls Modal functions
2. Modal runs the simulation
3. Modal returns results to our worker
4. Our worker stores results in Cloud SQL

**Changes Required**:

1. **Network Connectivity**: Cloud Run can call Modal's public API. No special configuration needed.

2. **File Transfer**: Currently, FMUs are stored in S3. We need to:

   - Update `sv_modal` to support reading from GCS
   - Or continue using S3 as a "neutral" storage location Modal can access
   - Or upload FMUs to Modal Volumes (current approach for caching)

3. **Credentials**: Modal API tokens stored in Secret Manager, injected to Cloud Run.

**Optional Future Enhancement**: Consider running simple validators (JSON Schema, XML Schema) on Cloud Run Jobs for parallelism, but this isn't necessary initially.

### Phase 8: Database Migration

**Goal**: Migrate production data from Heroku Postgres to Cloud SQL.

**Migration Steps**:

1. **Pre-Migration Preparation**:

   - Take a full backup of Heroku Postgres
   - Verify Cloud SQL instance is sized appropriately
   - Test migration process with staging data

2. **Announce Maintenance Window**:

   - Schedule 30-60 minute maintenance window
   - Notify active users

3. **Cutover Sequence**:

   ```bash
   # 1. Enable Heroku maintenance mode
   heroku maintenance:on -a validibot

   # 2. Create final dump
   heroku pg:backups:capture -a validibot
   heroku pg:backups:download -a validibot

   # 3. Restore to Cloud SQL
   gcloud sql import sql validibot-prod gs://bucket/dump.sql \
     --database=validibot

   # 4. Verify data
   gcloud sql connect validibot-prod --database=validibot
   > SELECT count(*) FROM validations_validationrun;

   # 5. Update Cloud Run to use new DB (already configured)

   # 6. Run migrations if needed
   gcloud run jobs execute migrate --region australia-southeast1

   # 7. Smoke test
   # - Create test validation
   # - Verify UI works
   # - Check API endpoints

   # 8. Update DNS (see Phase 9)
   ```

4. **Media File Sync**:
   ```bash
   # Sync S3 to GCS
   gsutil -m rsync -r s3://validibot-media gs://validibot-au-media
   ```

### Phase 9: DNS and TLS

**Goal**: Point production domain to Cloud Run.

**Steps**:

1. **Configure Cloud Run Custom Domain**:

   ```bash
   gcloud run domain-mappings create \
     --service validibot-web \
     --domain app.validibot.com \
     --region australia-southeast1
   ```

2. **Update DNS Records**:
   Add the CNAME record provided by Cloud Run to your DNS provider.

3. **TLS Certificate**:
   Cloud Run provides managed TLS certificates automatically.

4. **DNS Structure**:
   - `app.validibot.com` → Cloud Run web service
   - `api.validibot.com` → Same, or could be a separate mapping
   - Future: `au.validibot.com`, `us.validibot.com` for regional routing

---

## 6. Service-by-Service Migration Guide

### PostgreSQL: Heroku Postgres → Cloud SQL

**Why Cloud SQL?**

- Managed PostgreSQL, same as Heroku
- Located in Australian region
- Automated backups, high availability options
- Pay-per-use pricing

**Connection Changes**:

| Aspect     | Heroku                 | Cloud SQL                          |
| ---------- | ---------------------- | ---------------------------------- |
| Connection | `DATABASE_URL` env var | Unix socket or Cloud SQL Connector |
| SSL        | Automatic              | Configurable                       |
| Backups    | Manual or scheduled    | Automatic with PITR                |
| Scaling    | Provision larger plan  | Change instance tier               |

**Django Settings**:

```python
# Heroku (current)
DATABASES = {"default": env.db("DATABASE_URL")}

# Cloud SQL (new)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": f"/cloudsql/{GCP_PROJECT}:{GCP_REGION}:{INSTANCE_NAME}",
        "NAME": "validibot",
        "USER": env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
    }
}
```

### Redis → Cloud Tasks

**Why Remove Redis?**

Redis was only used as a Celery message broker. Cloud Tasks provides:

- Managed queue infrastructure (no broker to maintain)
- Built-in retries and dead-letter handling
- IAM-based authentication
- Native integration with Cloud Run

**What Changes**:

| Celery Concept       | Cloud Tasks Equivalent           |
| -------------------- | -------------------------------- |
| `@shared_task`       | HTTP endpoint + enqueue helper   |
| `task.apply_async()` | `CloudTasksClient.create_task()` |
| Worker process       | Cloud Run service                |
| Celery Beat          | Cloud Scheduler                  |
| Flower (monitoring)  | Cloud Console / Cloud Monitoring |
| Result backend       | Database (already doing this)    |

### AWS S3 → Cloud Storage

**Why Switch?**

- Unified billing and access control with GCP
- Australian region for data sovereignty
- Consistent tooling (gcloud CLI vs aws CLI)

**Django Storages Configuration**:

```python
# Current (S3)
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {"bucket_name": "validibot-media"},
    },
}

# New (GCS)
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": "validibot-au-media",
            "file_overwrite": False,
        },
    },
}
```

### Static Files

**Options**:

1. **Keep Whitenoise** (current approach):

   - Static files bundled in container image
   - Served by Django/Gunicorn
   - Simple, no external dependencies
   - Good for small static sets

2. **Move to Cloud Storage + CDN**:
   - `collectstatic` uploads to GCS bucket
   - Cloud CDN caches at edge locations
   - Better for large static assets
   - Reduces container size

**Recommendation**: Start with Whitenoise, consider CDN later for performance.

---

## 7. Security Considerations

### Cloud Tasks Endpoint Protection

The worker service handles sensitive operations. We protect it with multiple layers:

1. **Cloud Run IAM**:

   - Worker service set to "require authentication"
   - Only the Cloud Tasks service account has `roles/run.invoker`
   - Public cannot reach the worker at all

2. **OIDC Token Verification**:

   - Cloud Tasks includes an OIDC token in each request
   - Django verifies the token signature and audience
   - Rejects requests without valid tokens

3. **Shared Secret (Defense in Depth)**:
   - `django-cloud-tasks` uses a shared secret header
   - Additional verification layer

**Implementation**:

```python
def verify_cloud_tasks_request(request):
    """Verify the request came from Cloud Tasks."""

    # Check OIDC token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False

    token = auth_header[7:]
    try:
        # Verify with Google's public keys
        claims = google.oauth2.id_token.verify_token(
            token,
            google.auth.transport.requests.Request(),
            audience=settings.CLOUD_RUN_WORKER_URL,
        )
        # Verify service account
        if claims.get("email") != settings.CLOUD_TASKS_SERVICE_ACCOUNT:
            return False
    except Exception:
        return False

    # Also check shared secret
    secret = request.headers.get("X-DCT-SECRET")
    if secret != settings.DJANGO_CLOUD_TASKS_HANDLER_SECRET:
        return False

    return True
```

### Secret Management

All secrets stored in Google Secret Manager:

- `DJANGO_SECRET_KEY`
- `DB_PASSWORD`
- `CLOUD_TASKS_SECRET`
- `POSTMARK_SERVER_TOKEN`
- `SENTRY_DSN`
- `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`

Injected into Cloud Run as environment variables at deploy time.

### Network Security

- Cloud Run services run in Google's managed VPC
- Cloud SQL accessible via private IP or Cloud SQL Connector
- No public IP on Cloud SQL instance
- Egress to Modal.com allowed (external API calls)

### Data Encryption

- Cloud SQL: Encryption at rest (automatic)
- Cloud Storage: Encryption at rest (automatic)
- HTTPS: TLS for all traffic (managed certificates)

---

## 8. Local Development Changes

### What's Already Done

Local development now uses Docker Compose with:

- Django web server on port 8000
- Django worker server on port 8001 (simulating Cloud Run worker)
- PostgreSQL on port 5432
- Mailpit for email testing on port 8025

### Running Locally

```bash
# Start everything
make up

# Or with rebuild
make build

# View logs
make logs

# Stop
make down
```

### Simulating Cloud Tasks Locally

For local development, we don't need actual Cloud Tasks. Options:

1. **Eager Execution** (current Celery approach):

   ```python
   # settings/local.py
   DJANGO_CLOUD_TASKS_EXECUTE_LOCALLY = True
   ```

   Tasks execute synchronously in the same process.

2. **Local Worker Call**:
   When a task is enqueued, make an HTTP call to the local worker service:

   ```python
   if settings.DEBUG:
       requests.post(f"http://localhost:8001/internal/run-validation/{id}/")
   ```

3. **Full Emulation**:
   Use the Cloud Tasks emulator (more complex, usually not needed).

### Environment Variables

Local development uses `.envs/.local/.django` with:

```bash
DJANGO_SETTINGS_MODULE=config.settings.local
CELERY_TASK_ALWAYS_EAGER=True
DJANGO_CLOUD_TASKS_EXECUTE_LOCALLY=True
```

---

## 9. Testing Strategy

### Unit Tests

No changes needed for unit tests. They mock external services and test business logic.

### Integration Tests

For testing Cloud Tasks integration:

```python
# tests/test_cloud_tasks.py

@pytest.fixture
def mock_cloud_tasks(mocker):
    """Mock Cloud Tasks client."""
    mock = mocker.patch("google.cloud.tasks_v2.CloudTasksClient")
    return mock.return_value

def test_validation_enqueues_task(mock_cloud_tasks, db):
    """Verify validation run enqueues a Cloud Task."""
    workflow = WorkflowFactory()
    submission = SubmissionFactory()

    service = ValidationRunService()
    result = service.launch(request, workflow, submission)

    # Verify task was enqueued
    mock_cloud_tasks.create_task.assert_called_once()
    call_args = mock_cloud_tasks.create_task.call_args
    assert "/internal/run-validation/" in call_args.kwargs["task"]["http_request"]["url"]
```

### Staging Environment

Before production migration:

1. Deploy to a staging GCP project
2. Migrate a copy of production data
3. Run full test suite
4. Manual smoke testing
5. Load testing to verify performance

---

## 10. Rollback Plan

### During Migration

If issues arise during the migration window:

1. **DNS Rollback**: Point DNS back to Heroku (takes 5-60 minutes to propagate)
2. **Heroku Maintenance Off**: `heroku maintenance:off`
3. **Data Sync**: If any writes happened on GCP, sync back to Heroku (manual process)

### After Migration

For the first week after cutover:

1. Keep Heroku app running (read-only or maintenance mode)
2. Maintain ability to restore DNS
3. Daily backups of Cloud SQL exportable to Heroku if needed

### Permanent Rollback

If fundamental issues require returning to Heroku:

1. Export Cloud SQL database
2. Restore to Heroku Postgres
3. Sync GCS files back to S3
4. Redeploy Heroku app
5. Update DNS

---

## 11. Timeline and Milestones

### Phase 1: Foundation (Current - Week 1)

- [x] Create GCP project and enable APIs
- [x] Create Cloud Storage buckets
- [x] Containerize application for local development
- [x] Update production settings for GCP (Cloud Storage)
- [x] Configure IAM service accounts for dev/prod
- [x] Grant bucket-level permissions to service accounts
- [ ] Add Cloud Tasks URL routes

### Phase 2: Infrastructure (Week 2)

- [ ] Provision Cloud SQL instance
- [ ] Configure Secret Manager
- [ ] Build production Docker image
- [ ] Deploy staging Cloud Run services

### Phase 3: Task Migration (Week 3)

- [ ] Create Cloud Tasks queues
- [ ] Implement task enqueue helpers
- [ ] Create worker endpoint views
- [ ] Test end-to-end in staging

### Phase 4: Production Cutover (Week 4)

- [ ] Migrate database
- [ ] Sync media files
- [ ] Deploy production services
- [ ] Update DNS
- [ ] Monitor and stabilize

### Phase 5: Cleanup (Week 5+)

- [ ] Remove Celery code paths
- [ ] Decommission Heroku
- [ ] Document operational procedures
- [ ] Set up monitoring dashboards

---

## 12. Open Questions

### Events System

Currently, `validibot/events/` defines event types (`AppEventType`) but the models file is empty. Questions:

1. **Do we need a persistent event store?**

   - Current: Events are logged via `TrackingEventService` to `TrackingEvent` model
   - Option: Use Cloud Pub/Sub for event streaming
   - Option: Use Firestore for event storage
   - Recommendation: Keep using PostgreSQL (`TrackingEvent`) for now; evaluate Pub/Sub later for external integrations

2. **Webhook delivery**:
   - If we add webhooks for customers, Cloud Tasks is a natural fit for reliable delivery

### Infrastructure as Code

1. **Terraform vs Manual**:
   - Start manually for speed
   - Backfill Terraform after stabilization
   - Target: Terraform all resources by end of month 1

### Monitoring and Alerting

1. **Sentry** (keep): Application errors, performance
2. **Cloud Monitoring** (add): Infrastructure metrics, uptime checks
3. **Alert Policies**:
   - 5xx rate above 1% for 5 minutes
   - P95 latency above 2 seconds
   - Cloud SQL CPU above 80%
   - Cloud Tasks failure rate spike

### Multi-Region (Future)

Not in scope for this migration, but architecture supports:

- Duplicate entire stack per region
- Organization-level `data_region` field
- Region-specific domains (`au.validibot.com`, `us.validibot.com`)

---

## 13. Glossary

**Cloud Run**: Google's serverless container platform. Runs Docker containers that scale to zero.

**Cloud SQL**: Google's managed relational database service. We use PostgreSQL.

**Cloud Storage**: Google's object storage service (like AWS S3). Stores files in "buckets."

**Cloud Tasks**: Google's managed task queue service. Stores tasks and delivers them to HTTP endpoints with retries.

**Cloud Scheduler**: Google's managed cron service. Runs jobs on a schedule.

**Secret Manager**: Google's service for storing sensitive configuration securely.

**OIDC Token**: OpenID Connect token. Cloud Tasks includes these in requests to verify identity.

**Modal.com**: Third-party serverless compute platform optimized for heavy workloads. We use it for simulations.

**Celery**: Python distributed task queue library. Uses a broker (Redis) to send tasks to workers.

**uv**: Fast Python package installer and resolver. We use it instead of pip.

**Gunicorn**: Python WSGI HTTP server. Runs our Django application.

**Whitenoise**: Library that serves static files from Django. Bundles files into the container.

**django-storages**: Django library providing storage backends for cloud services (S3, GCS, etc.).

**PITR**: Point-in-Time Recovery. Cloud SQL feature to restore database to any moment in time.

---

## Appendix A: File Changes Summary

### New Files to Create

1. `compose/production/django/Dockerfile` - Production Docker image
2. `config/settings/gcp.py` - GCP-specific settings (or update `production.py`)
3. `validibot/validations/services/task_queue.py` - Cloud Tasks helpers
4. `validibot/validations/views_internal.py` - Worker endpoints
5. `validibot/validations/urls_internal.py` - Internal URL routes
6. `cloudbuild.yaml` - Cloud Build configuration
7. `docs/dev_docs/deployment/gcp.md` - GCP deployment guide

### Files to Modify

1. `config/urls.py` - Add Cloud Tasks routes
2. `config/settings/production.py` - GCP services configuration
3. `validibot/validations/services/validation_run.py` - Replace Celery with Cloud Tasks
4. `validibot/validations/tasks.py` - Deprecate, keep for reference

### Files to Eventually Remove

1. `config/celery_app.py` - Celery configuration
2. `Procfile` - Heroku-specific (keep until Heroku decommissioned)
3. `requirements/*.txt` - Once fully off Heroku

---

## Appendix B: Cost Comparison (Estimated)

| Service        | Heroku (Current)     | GCP (Target)                    |
| -------------- | -------------------- | ------------------------------- |
| Web Compute    | $50/mo (Standard 1x) | ~$20-40/mo (Cloud Run)          |
| Worker Compute | $50/mo (Standard 1x) | Included in Cloud Run           |
| Database       | $50/mo (Standard-0)  | ~$15-30/mo (Cloud SQL f1-micro) |
| Redis          | $15/mo (Hobby)       | $0 (not needed)                 |
| File Storage   | $5/mo (S3)           | ~$5/mo (GCS)                    |
| **Total**      | **~$170/mo**         | **~$40-80/mo**                  |

_Estimates based on low-moderate usage. Cloud Run pricing is usage-based, so costs scale with traffic._

---

## Appendix C: Useful Commands

### GCP CLI

```bash
# Authenticate
gcloud auth login
gcloud config set project validibot

# View Cloud Run services
gcloud run services list --region australia-southeast1

# View logs
gcloud run services logs read validibot-web --region australia-southeast1

# Deploy new revision
gcloud run deploy validibot-web --image gcr.io/validibot/app:latest

# View Cloud Tasks queues
gcloud tasks queues list --location australia-southeast1

# View pending tasks
gcloud tasks list --queue validation-runs --location australia-southeast1
```

### Database

```bash
# Connect to Cloud SQL
gcloud sql connect validibot-prod --database=validibot --user=validibot

# Run migrations
gcloud run jobs execute migrate --region australia-southeast1

# Create backup
gcloud sql backups create --instance validibot-prod
```

---

_This ADR will be updated as the migration progresses. Each phase completion should be noted with date and any deviations from the plan._

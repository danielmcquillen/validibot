# ADR-2025-12-01: Migrate Validibot Core Platform from Heroku to Google Cloud (Cloud Run)

**Status:** Accepted and Implemented (as of 2025-12-04)
**Date:** 2025-12-01
**Last Updated:** 2025-12-04
**Owner:** Daniel / Validibot Platform
**Related ADRs:** Pricing & Billing, Data Residency, Async Execution Model, Modal Integration, Validator Job Interface (2025-12-04)

---

## 1. Context

Validibot (formerly validibot) currently runs on:

- **Heroku (Common Runtime)** – Django app, 30s request timeout.
- **Heroku Postgres** – primary relational store.
- **Celery + Redis** – background workers, used even for basic validations because of Heroku’s 30s limit.
- **AWS S3** – static assets and uploaded files.
- **Modal.com** – heavy/CPU-bound validations and simulations (FMU, EnergyPlus, etc.).

Pain points and constraints:

1. **Heroku 30s timeout forces Celery for basic work**  
   Even simple validations must be offloaded to Celery to avoid router timeouts.

2. **No AU app region on Heroku Common Runtime**

   - App and DB run in US/EU; only S3 can be AU.
   - Harder to offer an “AU-first” data residency story.

3. **Celery + Redis adds operational weight**

   - Extra moving parts: broker, worker dynos, monitoring, scaling.
   - For most “basic” validations this is overkill if the platform can handle longer requests / queueing natively.

4. **Multi-region future (AU, US, EU)**

   - We eventually want regional stacks (AU-first, then US/EU), but **not** a distributed global DB.
   - Heroku Common Runtime (US/EU only) doesn’t map cleanly to an AU-anchored strategy.

5. **Solo founder bandwidth**
   - We need infra that scales but doesn’t turn the project into a DevOps job.
   - Strong preference for “serverless-ish” managed services and simple primitives.

Given this, we are considering a move to **Google Cloud** as the primary hosting platform while **continuing to use Modal.com** for heavy compute in the short–medium term.

---

## 2. Decision

We will migrate Validibot from Heroku to **Google Cloud Platform** with the following target architecture:

### 2.1 Target Architecture (AU-first)

**Region**

- Primary region: `australia-southeast1` (Sydney) for **MVP and AU customers**.
- Later: duplicate stack in a US region and an EU region for regional customers.

**Core services**

- **Compute:**

  - **Cloud Run (fully managed)** as the primary runtime for the Django web/API app and internal worker endpoints (no Celery workers).
  - Containers built via Cloud Build and stored in Artifact Registry.

- **Database:**

  - **Cloud SQL for PostgreSQL** in the same region as the app.

- **Object Storage:**

  - **Cloud Storage** buckets per region for uploads, validator inputs/outputs, and reports.
  - `django-storages` integration for media; static assets served either from Cloud Storage or out of the container.

- **Async / Background:**

  - **Cloud Tasks** for queueing and retrying background jobs (replacement for most Celery use cases).
  - **Cloud Run HTTP worker endpoints** as task targets (same Django app / container).
  - **Cloud Scheduler** for cron-like jobs (replacement for Celery Beat).

- **Secrets & Config:**

  - **Secret Manager** for DB passwords, API keys, Django SECRET_KEY, etc.
  - Environment variables injected into Cloud Run for config.

- **Heavy Compute / Simulations:**
  - **Modal.com remains the execution layer** for FMU, EnergyPlus, and other heavy validators.
  - Validibot orchestrates Modal jobs; Modal writes results back to S3/GCS or returns them directly.

**Data model / tenancy**

- Each **Organization** gets a **`data_region`** flag: `"AU" | "US" | "EU"` (initially all `"AU"`).
- **One Postgres DB per region** (no cross-region DB).
- **One Cloud Storage bucket per region** for org files.
- Job state (`ValidationJob`, `SimulationJob`, etc.) lives in Postgres, not in cloud-specific metadata.

### 2.2 Async Execution Model

We will **retire Celery** in the new stack and standardise on:

- Synchronous execution for **small/basic validations** that complete quickly.
- Asynchronous “job” execution via **Cloud Tasks + Cloud Run** for:
  - Larger “basic” validations.
  - Glue logic around Modal jobs (e.g. pre/post-processing, ingesting outputs).

Pattern:

1. API call creates a `ValidationJob` row (`PENDING`).
2. Enqueue a Cloud Task targeting `/internal/run-validation/<job_id>`.
3. Worker endpoint (Django view):
   - Loads job row (ORM).
   - Updates status → `RUNNING`.
   - Runs validator logic; writes outputs to GCS or DB.
   - Updates status → `SUCCESS` or `FAILED` + error details.
4. Frontend polls `/api/validations/<job_id>` or uses push channels for UX.

---

## 3. Alternatives Considered

### A. Stay on Heroku + Celery + Modal

- **Pros**

  - Minimal change in the short term.
  - Familiar stack; fast to keep shipping features.

- **Cons**
  - Still limited by 30s Heroku timeout → Celery required even for basic work.
  - No AU app region; poor fit with AU-first story.
  - Celery/Redis overhead remains.
  - Migration to multi-region later is awkward.

**Reason rejected:** Kicks core infra problems down the road and doesn’t solve AU hosting or simplify async.

---

### B. Move to AWS (App Runner / ECS + RDS + S3 + SQS/Λ) + Modal

- **Pros**

  - One cloud (AWS) with S3, RDS, KMS, etc.; strong enterprise story.
  - `ap-southeast-2` (Sydney) region covers AU hosting.
  - Rich ecosystem for future compliance/security.

- **Cons**
  - Higher conceptual overhead: SQS + Lambda/ECS/App Runner + EventBridge, etc.
  - More services to learn and glue together; heavier for a solo founder.
  - DX is rougher than Cloud Run/Tasks for containerised workloads.

**Reason rejected for MVP:** Overkill for current needs; slower path to a clean, maintainable async model.

---

### C. DigitalOcean App Platform (SYD1) + Managed Postgres + Spaces + Celery/Workers

- **Pros**

  - Heroku-like simplicity and lower costs.
  - Sydney region, meets AU hosting goal.
  - Familiar PaaS feel.

- **Cons**
  - No first-party equivalent of Cloud Tasks / Cloud Run Jobs.
  - Likely still relying on Celery or rolling our own worker orchestration.
  - Less mature ecosystem and compliance story vs GCP/AWS.

**Reason rejected:** Good PaaS option, but doesn’t give the same serverless async primitives as GCP.

---

## 4. Consequences

### 4.1 Positive

- **Heroku 30s timeout disappears:**  
  Cloud Run supports much longer request timeouts; basic validations can often run inline or via Cloud Tasks, removing the “everything must be Celery” constraint.

- **Simpler async story:**  
  Cloud Tasks + Cloud Run are enough for almost all “basic” validations. No separate worker dynos, no broker, no workers to babysit.

- **AU-first hosting:**  
  App, DB, and object storage live in an AU region from day one (plus Modal). Clear story for AU customers.

- **Better multi-region path:**  
  Same container + same schema can be deployed to US/EU regions later as independent stacks, keyed off `data_region`.

- **Cleaner separation of concerns:**
  - Django apps handle orchestration, state, and business logic.
  - Modal handles heavy sims.
  - GCP provides simple primitives (compute/DB/storage/queue/secrets).

### 4.2 Negative / Risks

- **Migration complexity:**

  - Must containerise the app and thoroughly test on Cloud Run.
  - Postgres data and media files must be migrated from Heroku/S3 to Cloud SQL/Cloud Storage with careful cutover.

- **Platform learning curve:**

  - Need to learn GCP basics: IAM, service accounts, Cloud Run, Cloud SQL, Cloud Tasks, networking.
  - Mistakes here can cause downtime or security exposure.

- **Multi-cloud reality:**

  - App stack on GCP, heavy compute on Modal (AWS under the hood) ⇒ multi-cloud from day one.
  - Requires clear documentation and DPAs/Subprocessors section.

- **Potential over-investment in infra vs product:**
  - Time spent on migration is time not spent on features, marketing, and sales.

### 4.3 Mitigations

- **Phased migration** (see §5) with a fully working staging environment before cutover.
- **Keep Heroku running** for a transition period as a fallback (read-only if needed).
- **Container-first design** so moving to AWS (App Runner/ECS) later is feasible if needed.
- **Thin cloud-specific abstraction layers** for queueing and storage to keep vendor lock-in manageable.

---

## 5. Implementation & Migration Plan

High-level phases, in the order we’ll execute them.

### Phase 0 – Clarify non-goals and constraints

- We **accept**:

  - Modal as a US-based subprocessor (for now).
  - Single-region per stack (no cross-region DB replication).
  - A scheduled maintenance window during cutover (no need for zero-downtime multi-master magic).

- We **will not**:
  - Implement cross-region distributed DB.
  - Rebuild Modal on GCP in this ADR (that’s a separate decision later).

---

### Phase 1 – Prepare app for containerisation

1. Ensure Django config is **12-factor friendly**:

   - All secrets and env-specific config via environment variables.
   - No Heroku-specific magic baked into settings.

2. Separate concerns in code where needed:

   - Storage access via a `storage` module (so we can swap S3 → GCS).
   - Queue access via a `queue` module (Celery → Cloud Tasks later).

3. Remove or cordon off Heroku-specific add-ons/paths (e.g. `django-heroku`).

Deliverable: codebase ready to run entirely from a Docker image using env vars.

---

### Phase 2 – Build Docker image and run locally

1. Create `Dockerfile` for the Django app (gunicorn + static collection).
2. Create `docker-compose.yml` for local dev (Postgres + app container).
3. Verify:
   - App runs locally in container.
   - Migrations apply cleanly.
   - Tests pass.
   - Static assets and media are handled correctly.

Deliverable: reproducible container image for Validibot.

---

### Phase 3 – Provision GCP resources (staging)

In a dedicated **staging** GCP project:

1. Create **Cloud SQL Postgres** (same major version as Heroku Postgres).
2. Create **Cloud Storage** bucket for staging uploads/reports.
3. Create a **Cloud Run** service from the Docker image:
   - Region: `australia-southeast1`.
   - Connect to Cloud SQL via connector.
   - Configure env vars for DB and storage.
4. Set up **Secret Manager** for DB credentials, SECRET_KEY, etc.
5. Configure **logging and basic alerting** (error rate, CPU, 5xx).

Deliverable: fully working staging environment on GCP, independent of Heroku.

---

### Phase 4 – Introduce job-based async model (staging)

In staging, not in prod yet:

1. Add `ValidationJob` and `SimulationJob` models + admin views.
2. Implement public APIs:
   - `POST /api/validations` → create job row.
   - `GET /api/validations/<id>` → read status + summary.
3. Create **Cloud Tasks** queue for validations.
4. Add helper in Django to enqueue tasks targeting `/internal/run-validation/<job_id>`.
5. Add internal worker endpoint(s) for Cloud Tasks:
   - Implement basic validators (JSON/XML/CEL) as functions used both by synchronous and async paths.
   - Ensure idempotence and good error handling.

Deliverable: Cloud Tasks + Cloud Run worker pattern working end-to-end in staging.

---

### Phase 5 – Wire in Modal from GCP (staging)

1. Verify Modal client usage from the Cloud Run environment (networking, auth).
2. Implement orchestration functions:

   - Create `SimulationJob` row.
   - Kick off Modal job with job ID and input URIs.
   - Handle completions (poll or callback) and update job row.

3. Ensure outputs from Modal are written to GCS (not S3) for the GCP stack.

Deliverable: end-to-end heavy validator flow working from GCP staging.

---

### Phase 6 – Data migration plan (prod)

Design a **simple, downtime-acceptable** migration from Heroku → Cloud SQL + S3 → GCS:

1. **Dry run migration in staging**:

   - Take a snapshot `pg_dump` from Heroku staging DB, restore into Cloud SQL.
   - Copy S3 staging files to GCS with matching keys.
   - Point staging app at migrated DB + bucket and validate behaviour.

2. **Cutover sequence for prod** (high-level):

   1. **Announce maintenance window** to early users (e.g. 30–60 min).
   2. Put Heroku app into **maintenance or read-only** mode.
   3. Take final `pg_dump` of Heroku Postgres (or use logical replication if we want to be fancy; not required).
   4. Restore dump into **Cloud SQL prod** instance.
   5. Sync S3 → GCS for prod buckets (delta from last big sync).
   6. Update Cloud Run prod environment vars to point at Cloud SQL + GCS.
   7. Smoke-test prod GCP app using internal admin / test account.
   8. Flip DNS (CNAME / A) for `app.validibot.com` (or equivalent) to GCP (Cloud Run / load balancer).
   9. Monitor logs, error rates, DB metrics.

   Heroku remains up but effectively unused; can be temporarily left in read-only as backup.

Deliverable: clear step-by-step migration runbook.

---

### Phase 7 – DNS & TLS

1. Decide on DNS structure (e.g. `app.validibot.com`, `au.validibot.com`).
2. Configure load balancing / Cloud Run domain mapping with:
   - Managed TLS certificates.
   - Appropriate redirects (e.g. `www → root`, HTTP → HTTPS).
3. Verify local and global access.

Deliverable: external users login and use Validibot via GCP stack.

---

### Phase 8 – Remove Celery & Heroku dependencies

After GCP prod has been stable for a bit:

1. Route **all new validation flows** through the job-based async model (Cloud Tasks).
2. Remove Celery tasks from the core app or mark as deprecated for a release or two.
3. Decommission Celery workers and Redis on the new stack (don’t provision them in GCP at all).
4. Once confident:
   - Tear down Heroku app and add-ons (or downgrade to a minimal plan for emergency rollback window).

Deliverable: simplified async architecture; no Celery or Heroku dependencies.

---

### Phase 9 – Prepare for multi-region (post-MVP)

This ADR only commits to **AU stack**, but we will:

1. Keep `Organization.data_region` field in place with `"AU"` default.
2. Keep infra code parameterised by region (DB instance name, bucket names).
3. Later, to support US/EU:
   - Create new GCP projects/regions (US/EU).
   - Replicate the same stack (Cloud Run + Cloud SQL + GCS + Cloud Tasks).
   - Use DNS like `us.validibot.com`, `eu.validibot.com` pointing to respective stacks.
   - New orgs in those regions are created in the regional DBs.

---

## 5A. Implementation Status (Updated 2025-12-04)

All phases have been completed. Here's the final status of each phase:

### Phase 1 – App Containerization ✅ COMPLETED

**Completed:** Early December 2025

- Migrated all Django configuration to environment variables using `django-environ`
- Removed Heroku-specific dependencies (`django-heroku`)
- Separated storage and queue abstractions
- Verified all settings are 12-factor compliant

### Phase 2 – Docker Image ✅ COMPLETED

**Completed:** Early December 2025

- Created production `Dockerfile` with multi-stage build
- Added `docker-compose.yml` for local development
- Configured `gunicorn` with appropriate workers and timeout settings
- Tested migrations, tests, and static asset collection in containers

### Phase 3 – GCP Resources (Staging) ✅ COMPLETED

**Completed:** December 2025

- Provisioned Cloud SQL PostgreSQL in `australia-southeast1`
- Created GCS buckets (`validibot-media`, `validibot-files`)
- Deployed Cloud Run service with Cloud SQL connector
- Configured Secret Manager for credentials
- Set up Cloud Logging and basic monitoring

### Phase 4 – Cloud Run Jobs for Validators ✅ COMPLETED

**Completed:** December 4, 2025

**Key Achievement:** Replaced Cloud Tasks pattern with Cloud Run Jobs for heavy validators

Instead of implementing Cloud Tasks for all async work, we adopted a **hybrid approach**:

- **Synchronous execution** for lightweight validators (JSON, XML, CEL)
- **Cloud Run Jobs** for heavy compute validators (EnergyPlus, FMU)
- **Callback pattern** for async notification when jobs complete

**Implementation Details:**

1. **Validator Job Interface** (See ADR 2025-12-04):validibot

   - Created `ValidationInputEnvelope` and `ValidationOutputEnvelope` schemas in `vb_shared`
   - Defined typed subclasses for domain-specific validators (EnergyPlusInputEnvelope, etc.)
   - Implemented callback-based async pattern (POST callback when complete)

2. **Cloud Run Job Launcher Service** ([lauvalidibotlevalidations/validations/services/launcher.py)):

   - `launch_energyplus_validation()` - Orchestrates EnergyPlus Cloud Run Jobs
   - Uploads submission files to GCS
   - Builds typed input envelopes and callback URLs protected by Cloud Run IAM
   - Triggers Cloud Run Jobs via Cloud Tasks (queue OIDC token invokes Cloud Run Jobs API)
     validibot

3. **GCS Integration** ([gcs_client.py](../validibot/validations/services/gcs_client.py)):

   - `upload_envelope()` - Upload Pydantic envelopes as JSON to GCS
   - `download_envelope()` - Download and validate envelopes from GCS
   - `upload_file()` - Upload arbitrary filvalidibot
   - `parse_gcs_uri()` - Parse gs:// URIs into bucket/path components

4. **Envelope Builder** ([envelope_builder.py](../validibot/validations/services/envelope_builder.py)):

   - `build_energyplus_input_envelope()` - Construct typed input envelopes
   - Includes validator metadata, organization info, workflow context
   - Generates callback URLs scoped to the worker service (Cloud Run IAM)
   - Configures execution context (timeouts, tags, bundle URIs)

5. **Callback Handler** ([callbacks.py](../validibot/validations/api/callbacks.py)):

   - `ValidationCallbackView` - Django REST endpoint for validator callbacks
   - Verifies Google-signed ID tokens from the validator job service account
   - Downloads full output envelope from GCSvalidibot
   - Updates ValidationRun status and stores results
   - Maps validator status codes to ValidationRun status codes

6. **EnergyPlus Validator Container** ([validators/energyplus/](../validators/energyplus/)):

   - Standalone Python container deployed as Cloud Run Job
   - Downloads input envelope from GCS
   - Runs EnergyPlus simulation
   - Uploads output envelope to GCS
   - POSTs minimal callback to Django

7. **Engine Integration** ([energyplus.py](../validibot/validations/engines/energyplus.py)):
   - Added `validate_with_run()` method for async execution via Cloud Run Jobs
   - Checks Cloud Run Jobs configuration before launching
   - Falls back to synchronous execution if not configured

**Architecture Benefits:**

- No Celery/Redis infrastructure needed
- Type-safe communication via Pydantic envelopes
- Secure callbacks using Cloud Run IAM ID tokens (no shared secrets)
- Clean separation: Django orchestrates, Cloud Run Jobs execute
- GCS acts as durable state store for inputs/outputs

### Phase 5 – Modal Integration ⏸️ DEFERRED

**Status:** Modal integration for FMI validators remains but is marked for future migration

Modal.com is still used for FMI (Functional Mock-up Interface) validators. The Cloud Run Jobs pattern implemented in Phase 4 provides the blueprint for migrating FMI validators from Modal to Cloud Run Jobs when resources allow.

**Future Work:**

- Create FMI validator container (similar to EnergyPlus)
- Implement `launch_fmi_validation()` in launcher service
- Build FMIInputEnvelope and FMIOutputEnvelope schemas
- Deploy as Cloud Run Job in `australia-southeast1`

### Phase 6 – Data Migration ✅ COMPLETED

**Completed:** December 2025

- Successfully migrated Postgres data from Heroku to Cloud SQL
- Migrated media files from AWS S3 to GCS
- Zero data loss during migration
- Maintained downtime within acceptable window

### Phase 7 – DNS & TLS ✅ COMPLETED

**Completed:** December 2025

- Configured Cloud Run custom domain mapping
- Enabled Google-managed TLS certificates
- Set up appropriate redirects (www → root, HTTP → HTTPS)
- DNS cutover completed successfully

### Phase 8 – Remove Celery & Heroku ✅ COMPLETED

**Completed:** December 4, 2025

- Removed all Celery task definitions
- Decommissioned Celery workers
- Removed Redis dependency
- Cleaned up backward compatibility code
- All validators now use either:
  - Synchronous execution (lightweight)
  - Cloud Run Jobs (heavy compute)

**Backward Compatibility Cleanup:**

- Removed `InputItem` → `InputFileItem` alias
- Removed `ValidationResultEnvelope` → `ValidationOutputEnvelope` alias
- Removed `configure_modal_runner()` stub from FMI service
- Updated all tests to use current API
- Fixed schema version references (`validibot.output.v1`)

### Phase 9 – Multi-Region Preparation 🚧 IN PROGRESS

**Status:** Foundation laid, full multi-region deployment pending

**Completed:**

- `Organization.data_region` field exists with `"AU"` default
- Settings parameterized by region (bucket names, job names)
- All code region-aware

**Remaining:**

- Create US/EU GCP projects
- Replicate stack in `us-central1` and `europe-west1`
- Set up regional DNS (`us.validibot.com`, `eu.validibot.com`)
- Implement region-routing logic

---

## 6. Open Questions

1. **Infra-as-code:**

   - Do we formalise all GCP resources in Terraform / Pulumi from day one, or bootstrap manually then backfill IaC later?

2. **Monitoring & error reporting:**

   - Exact choices for metrics/alerts: Cloud Monitoring only, or also Sentry/another APM?

3. **Modal data residency bounds:**

   - How much PII can/should Modal see?
   - Do we enforce a pattern where Modal only sees file IDs / GCS URIs and no user identifiers?

4. **Legacy S3 usage:**

   - Do we migrate _all_ S3 usage for Validibot to GCS, or keep S3 around for some non-critical assets?

5. **Roll-back strategy:**
   - For the first week after cutover, do we keep Heroku ready for a full rollback, or do we rely on Cloud SQL snapshots + backups only?

---

## 7. Gaps to fill (libraries, security, settings)

- **Task queue & retries:** Use `google-cloud-tasks` (Python SDK). Enqueue tasks via `transaction.on_commit` to avoid running on rolled-back rows. Configure retries/backoff/dead-letter queues per queue. Worker endpoints must validate Cloud Tasks OIDC token (audience set to the service URL) and reject unauthenticated requests.
- **Storage:** Use `django-storages[google]` with GCS buckets per region. Set `STATIC_URL`/`MEDIA_URL` to GCS/Cloud CDN; remove Heroku/Whitenoise assumptions. Configure CORS for uploads if needed.
- **DB connectivity:** Cloud SQL Postgres via connector (Unix socket preferred in Cloud Run). Tune Cloud Run concurrency and connection pool to respect Cloud SQL connection limits; consider pgBouncer if needed later.
- **Secrets:** Store secrets in Secret Manager; inject into Cloud Run as env vars. Drop `django-heroku` and any Heroku-specific settings.
- **Observability:** Keep Sentry (or equivalent) for app errors; use Cloud Monitoring alerts on 5xx rate/latency and Cloud Tasks failure rates. Ship structured logs to Cloud Logging.
- **Static/media CDN:** Decide on Cloud CDN/CloudFront equivalent for static assets; document in settings.
- **CSRF/hosts:** Update `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS` for Cloud Run domains and custom domains.
- **Modal callbacks:** Keep compute callbacks public but authenticated (HMAC or OIDC). Outputs go to region-scoped GCS paths. Log and reject unsigned callbacks.
- **Celery migration map:** Inventory existing Celery tasks/chains/beat schedules and map them to Cloud Tasks + Cloud Scheduler equivalents; document any patterns (chords/groups) and their replacements.

---

## 8. Workflow orchestration without Celery (Validibot-native model)

Celery’s chains/chords are not the product; Validibot needs its own workflow engine in Postgres. Cloud Tasks + Cloud Run execute steps; Django orchestrates state.

### Core model (sketch)

- `WorkflowDefinition` / `WorkflowStepDefinition` – user-authored graph of validators/actions.
- `WorkflowRun` – one execution of a workflow.
- `WorkflowStepRun` – one step run; fields: type (`VALIDATOR`/`ACTION`), status (`PENDING`, `RUNNING`, `WAITING`, `SUCCESS`, `FAILED`), `depends_on` group/ids, outputs/errors.

### Execution pattern

- `POST /api/workflows/<id>/runs` → create `WorkflowRun`, enqueue Cloud Task to `/internal/advance-workflow/<run_id>`.
- `advance-workflow` view:
  - Reads `WorkflowRun` + `WorkflowStepRun` rows.
  - Finds runnable steps (deps satisfied).
  - Enqueues `/internal/run-step/<step_run_id>` for each runnable step.
- `run-step` view:
  - Executes the step:
    - Simple validators inline.
    - Actions (Slack/NFT/VC): call external API with retries, update status.
  - Marks step success/fail; if completion unblocks others, enqueue `advance-workflow` again.
- Callback handlers mark long-running steps done/failed and re-enqueue `advance-workflow`.

### Why this replaces Celery chains

- Orchestration lives in your schema (inspectable, replayable, per-tenant).
- Cloud Tasks provides durable execution and retries; you are not locked to Celery primitives.
- Parallelism = enqueue multiple `run-step` tasks; fan-in = `depends_on` logic in DB.

### Google Workflows?

- Optional later. For MVP, keep orchestration in Django + Postgres + Cloud Tasks. Avoid generating Workflows YAML per user; too much coupling and drift risk. Evaluate Temporal/Prefect/Dagster later if needed.

---

## 9. Library and settings checklist (MVP)

- **Python deps** (add to `pyproject.toml` / `requirements`):

  - `google-cloud-tasks` (enqueue, manage queues)
  - `google-cloud-storage` + `django-storages[google]` (media/static on GCS)
  - `google-cloud-secret-manager` (optional if loading secrets at runtime)
  - `google-cloud-sql-connector` (optional if using connector library; else standard `psycopg` with Unix socket)
  - `gunicorn` (if not already present)
  - `psycopg`/`psycopg2` (Postgres driver)
  - `sentry-sdk` (if using Sentry)
  - Drop/avoid `django-heroku`; remove Celery deps once migrated.

- **Django settings** (per region):

  - `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` include Cloud Run domain + custom domains.
  - Database: Cloud SQL connection via Unix socket (`/cloudsql/<instance-connection-name>`) or TCP with connector; set pool/concurrency to respect Cloud SQL limits.
  - Storage: configure `DEFAULT_FILE_STORAGE` for GCS; set `STATIC_URL`/`MEDIA_URL` to GCS/CDN. Remove Whitenoise if using CDN/GCS for static.
  - Cloud Tasks: queue name(s), location, OIDC service account, target URLs; verification of `Authorization` header audience in worker views.
  - Secrets: load from env (populated from Secret Manager). Keep `DJANGO_SECRET_KEY`, DB credentials, API keys out of source.
  - Logging: JSON/structured logging to stdout for Cloud Logging; Sentry DSN if enabled.
  - Security: HMAC/oidc check for worker/compute callbacks; set appropriate CORS if needed for uploads.

- **Dev/prod parity**:

  - `docker-compose` for local with Postgres + optional fake GCS (or MinIO) to mimic storage.
  - Local task execution shim for Cloud Tasks (e.g., direct function call) to keep dev loop tight.

- **CI/CD**:
  - Cloud Build/GitHub Actions workflow to build/push image to Artifact Registry.
  - Migration runner step (`python manage.py migrate`) before/after deploying a new revision.
  - Optionally seed queues and ensure required Cloud Tasks queues exist (via IaC or startup check).

---

## 10. Filling the remaining gaps (detailed, step-by-step)

This section expands the missing pieces with explicit “how” so it doubles as a learning guide.

### Infra-as-code approach

We will bootstrap manually for the first AU stack (staging + prod) to move quickly, then backfill Terraform by a set date (choose a realistic date, e.g., end of the first stable month on GCP). Terraform should cover Cloud Run services, Cloud SQL instances, Cloud Storage buckets, Cloud Tasks queues, Secret Manager entries, and Cloud Scheduler jobs. This gives us reproducible environments when we spin up US/EU later.

### Monitoring and APM

We will use Sentry for application errors (uncaught exceptions, performance traces if we enable them) and Cloud Monitoring for infrastructure alerts. Minimum alerts to configure:

- HTTP 5xx rate above a small threshold for N minutes.
- P95 latency above a threshold (start with something like 1–2 seconds, tune as we learn).
- Cloud Tasks: increasing failure count or tasks age in queue (indicates stuck workers).
- Cloud SQL: high CPU/connections or storage nearing limits.

Sentry: wire the DSN in settings; ensure release tags are set from the build (e.g., Git SHA).

### Cloud Tasks limits, retries, and timeouts

Keep these constraints in mind:

- Task payload size: Cloud Tasks allows payloads up to 1 MB; keep payloads small (IDs, not blobs). For large inputs, write to GCS and pass URIs.
- Execution time: Cloud Run request timeout can be set (default 5 minutes; max 60). Set a sensible per-task timeout (e.g., 5–10 minutes) for “basic” work; heavy jobs go to Modal and are marked WAITING until callbacks arrive.
- Retries: Configure queues with backoff (e.g., exponential starting at a few seconds) and a max retry count. Use idempotent handlers that can run multiple times safely.
- Rate limits: Set max dispatch rate on queues to avoid stampedes against the DB. Start conservative, monitor, then raise.
- Dead-letter: Configure a dead-letter queue or at least log/alert on exhausted retries so we can inspect failed tasks.

### Endpoint authentication for Cloud Tasks and callbacks

Worker endpoints (`/internal/run-step/<id>`, `/internal/advance-workflow/<id>`) must only be callable by Cloud Tasks:

- In the Cloud Task definition, set the OIDC token with a service account you control and set the audience to the exact Cloud Run URL.
- In the Django view, validate the `Authorization: Bearer <token>` against Google’s public keys and check the audience matches your service URL. Reject anything without a valid token.
- Keep these endpoints under an `/internal/` prefix and do not expose them via frontend routes.

Modal/compute callbacks:

- Require an HMAC signature header (shared secret) or OIDC token issued by your own service account if Modal supports it. Verify signature before touching the body.
- Keep callbacks minimal: validate, update DB state, enqueue `advance-workflow` to continue orchestration.

### Backup and restore plan (Cloud SQL + GCS)

Cloud SQL (Postgres):

- Enable automated backups and point-in-time recovery (PITR). Set a retention window that matches your risk appetite (e.g., 7–14 days).
- Take on-demand backups before major schema changes or migrations.
- Test restore: periodically restore a backup into a staging instance and run smoke tests to prove backups are usable.

Disaster recovery steps:

1. If data corruption or accidental deletion occurs, create a new Cloud SQL instance from the latest backup or PITR timestamp.
2. Point the Cloud Run service to the restored instance (update env vars / instance connection name), run migrations if needed, and smoke test.
3. For regional outages, spin up the same stack in another region with the most recent backup. This is manual for now.

GCS (uploads/reports):

- Enable object versioning on the buckets to protect against accidental deletes/overwrites. Set a lifecycle policy to expire old versions after a reasonable period to control cost.
- Regularly test reading historical versions and restoring them.

### Scheduled tasks (Celery Beat equivalent)

We do not have Celery Beat tasks yet. When we add scheduled jobs (e.g., cleanup, digest emails), define them explicitly and run them with Cloud Scheduler calling dedicated endpoints (with auth) or by enqueueing Cloud Tasks on a schedule. Keep the list of scheduled jobs small and documented.

### Local development and Cloud Tasks emulation

For local dev, keep the feedback loop tight:

- Provide a simple “local tasks” shim: in local settings, calls to enqueue a Cloud Task can directly invoke the handler function or hit the endpoint synchronously. This avoids needing a local emulator.
- Alternatively, use the Cloud Tasks emulator if desired, but the direct-call shim is often simpler for Django apps.
- For tests, mock the enqueue function to capture intended tasks and assert they would be enqueued, or run handlers directly against the test database with transactional tests.

### Organization-to-region routing

Strategic approach:

- Each org has a `data_region` set at signup; the signup flow should select a region based on user choice or geolocation, and then create the org in that regional stack (DB, buckets, tasks).
- DNS and app entry points should guide users to the correct regional stack (e.g., `au.validibot.com`, `us.validibot.com`, `eu.validibot.com`).
- Cross-region data movement should be avoided; no mixing of DBs across regions.

MVP constraint:

- We are AU-only for MVP. Signups default to `data_region = "AU"`, and the app runs only in the AU stack. Other regions are blocked or redirected until those stacks exist.

### Static assets and media

Best practice for static in this stack:

- Prefer serving static files from GCS + Cloud CDN. This keeps the container lean and offloads bandwidth. Configure `STATIC_URL` to the CDN-backed GCS path; collectstatic uploads to GCS via `django-storages`.
- If you keep Whitenoise for now, be aware it only serves what is baked into the container image. That’s fine for small static sets, but long-term GCS+CDN scales better. Document the plan to move static to GCS when convenient.

Media (user uploads, reports):

- Use `django-storages[google]` to write/read from region-scoped GCS buckets. Set appropriate ACLs (private by default) and signed URL helpers if needed for downloads.
- Keep bucket names region-specific to align with `data_region`.

### File storage with django-storages (how to wire it)

1. Install `django-storages[google]` and `google-cloud-storage`.
2. In settings, set:
   - `DEFAULT_FILE_STORAGE = "storages.backends.gcloud.GoogleCloudStorage"`
   - `GS_BUCKET_NAME` to the region’s bucket (e.g., `validibot-au-media`).
   - `GS_DEFAULT_ACL = None` to keep files private; use signed URLs to serve files securely.
   - `STATICFILES_STORAGE` similarly if you move static to GCS.
3. Provide GCP credentials via service account (mounted in Cloud Run) or workload identity; avoid JSON keys in source.
4. Test upload/download in staging; verify paths and permissions.
5. Enable bucket versioning and lifecycle rules (see backup section).

### Next steps

- Choose the “backfill Terraform by” date and add it to this ADR.
- Wire Sentry and Cloud Monitoring alerts as described and capture thresholds.
- Define Cloud Tasks queue configs (timeouts, retry counts, rate limits) in IaC or code comments.
- Implement the local tasks shim and document how to run it in dev settings.
- Decide when to move static off Whitenoise to GCS+CDN and document the migration trigger.

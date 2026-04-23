# =============================================================================
# Validibot - Google Cloud Platform Deployment
# =============================================================================
#
# Commands for deploying and managing Validibot on Google Cloud Platform.
#
# Architecture:
#   - Cloud Run: Web and Worker services (containerized Django)
#   - Cloud SQL: PostgreSQL database (managed)
#   - Cloud Storage: Media and file storage + CMEK-encrypted audit log
#                    archive (GCS buckets)
#   - Cloud Tasks: Background job queue
#   - Cloud Scheduler: Cron-like scheduled tasks
#   - Secret Manager: Environment variables and secrets
#   - Cloud KMS: Cryptographic key management for credential signing
#   - Global Load Balancer: HTTPS termination and custom domains
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Docker configured for Artifact Registry: just gcp auth
#   - GCP config sourced: source .envs/.production/.google-cloud/.just
#
# Quick start (new environment):
#   source .envs/.production/.google-cloud/.just
#   just gcp init-stage dev           # Create infrastructure
#   # Create .envs/.dev/.google-cloud/.django manually (see .envs.example/)
#   just gcp secrets dev              # Upload secrets to Secret Manager
#   just gcp deploy-all dev           # Build, migrate, and deploy services
#   just gcp setup-data dev           # Seed initial data
#
# Daily operations:
#   source .envs/.production/.google-cloud/.just
#   just gcp deploy prod              # Deploy web service to prod
#   just gcp logs prod                # View logs
#   just gcp status prod              # Check service status
#
# =============================================================================

# Use bash for shell commands (more predictable than sh)
set shell := ["bash", "-cu"]

# Run recipes from the project root (not this submodule directory)
set working-directory := '../..'

# Ensure gcloud SDK is in PATH (needed for docker-credential-gcloud)
# This handles the case where gcloud is installed but not in PATH
export PATH := env_var("HOME") + "/google-cloud-sdk/bin:" + env_var("PATH")

# =============================================================================
# GCP Project Configuration
# =============================================================================
#
# Configuration is loaded from environment variables. Set these before running:
#
#   export GCP_PROJECT_ID="your-project-id"
#   export GCP_REGION="us-central1"
#
# Or source the config file:
#
#   source .envs/.production/.google-cloud/.just
#
# See .envs.example/.production/.google-cloud/.just for a template.
#
# =============================================================================

# Load from environment variables
# Use env_var_or_default with empty string to allow non-GCP commands to run.
# Actual GCP commands will fail with a clear error if not set.
gcp_project := env_var_or_default("GCP_PROJECT_ID", "")
gcp_region := env_var_or_default("GCP_REGION", "us-central1")

# Application name prefix for all GCP resource names.
# All resource names follow the pattern: {app_name}-{resource}[-{stage}]
# Set GCP_APP_NAME in .envs/.production/.google-cloud/.just or your environment.
app_name := env_var_or_default("GCP_APP_NAME", "validibot")

# Docker image path in Artifact Registry
# Format: {region}-docker.pkg.dev/{project}/{repository}/{image}
gcp_image := gcp_region + "-docker.pkg.dev/" + gcp_project + "/" + app_name + "/" + app_name + "-web"

# Cloud Run request timeout (1 hour)
# Long timeout needed for validator jobs that process large files
gcp_cloud_run_request_timeout := env_var_or_default("GCP_CLOUD_RUN_TIMEOUT", "3600s")

# Cloud Scheduler timezone for cron jobs
gcp_scheduler_timezone := env_var_or_default("GCP_SCHEDULER_TIMEZONE", "UTC")

# Validator container registry path
validator_repo := gcp_region + "-docker.pkg.dev/" + gcp_project + "/" + app_name

# Helper to check GCP_PROJECT_ID is set before running commands
[private]
_require-gcp-config:
    #!/usr/bin/env bash
    if [ -z "${GCP_PROJECT_ID:-}" ]; then
        echo "Error: GCP_PROJECT_ID is not set."
        echo ""
        echo "GCP deployment is not yet automated in CI. For now, deploy"
        echo "manually by sourcing your GCP config first:"
        echo ""
        echo "  source .envs/.production/.google-cloud/.just"
        exit 1
    fi

# Helper to block deploys when a stage is in maintenance mode.
# Checks the Cloud SQL instance status — if it's stopped, maintenance is on.
[private]
_require-not-maintenance stage:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then
        DB_INSTANCE="${APP_NAME}-db"
    else
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
    fi
    DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "unknown")
    if [ "$DB_STATUS" != "RUNNABLE" ]; then
        echo "Error: {{stage}} is in maintenance mode (database is $DB_STATUS)."
        echo ""
        echo "Deploying while maintenance is on would leave services unable to"
        echo "reach the database. Bring the stage online first:"
        echo ""
        echo "  just gcp maintenance-off {{stage}}"
        exit 1
    fi

# Git SHA for image tagging (inherited from common.just via root justfile)
git_sha := `git rev-parse --short HEAD`

# =============================================================================
# Service Account Configuration
# =============================================================================
#
# Each stage has its own service account for security isolation:
#   - {app_name}-cloudrun-prod@{project}.iam.gserviceaccount.com
#   - {app_name}-cloudrun-dev@{project}.iam.gserviceaccount.com
#   - {app_name}-cloudrun-staging@{project}.iam.gserviceaccount.com
#
# The app_name defaults to "validibot" and can be changed via GCP_APP_NAME.
#
# Service accounts have minimal permissions:
#   - cloudsql.client: Connect to Cloud SQL
#   - secretmanager.secretAccessor: Read secrets
#   - run.invoker: Call other Cloud Run services
#   - cloudtasks.enqueuer/viewer: Manage task queue
#   - storage.objectUser: Read/write to GCS buckets
#   - cloudkms.viewer/signerVerifier: Sign credentials with KMS
#
# =============================================================================

# Helper to compute service account email
[private]
_service-account stage:
    @if [ "{{stage}}" = "prod" ]; then \
        echo "{{app_name}}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"; \
    else \
        echo "{{app_name}}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"; \
    fi

# =============================================================================
# Build & Push
# =============================================================================

# Build Docker image for Cloud Run (linux/amd64 for Cloud Run on x86_64).
# Set GCP_BUILD_VALIDIBOT_CLOUD=1 to include the commercial layers (Pro + Cloud).
# Community users can ignore this flag.
#
# Safety: Before building, checks all repos for uncommitted changes. Building
# from a dirty working tree is dangerous because rsync copies whatever is on
# disk — you could accidentally deploy unreleased or broken code. The check
# covers the community repo plus Pro and Cloud (when included). Set
# GCP_ALLOW_DIRTY=1 to bypass in emergencies.
build: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail

    # ── Dirty-repo guard ────────────────────────────────────────────────
    # Docker images are tagged with the HEAD commit SHA. If the working
    # tree has uncommitted changes, the image contents won't match what
    # that SHA represents in git — making rollbacks unreliable and
    # debugging impossible ("but that commit doesn't have that code…").
    #
    # We check every repo that contributes code to the image:
    #   1. This repo (community — the Docker build context)
    #   2. validibot-pro and validibot-cloud (when GCP_BUILD_VALIDIBOT_CLOUD=1)
    #
    # In emergencies, set GCP_ALLOW_DIRTY=1 to skip this check.
    # ────────────────────────────────────────────────────────────────────
    if [ "${GCP_ALLOW_DIRTY:-0}" != "1" ]; then
        DIRTY_REPOS=()

        # Check community repo (always — it's the base image)
        if [ -n "$(git status --porcelain)" ]; then
            DIRTY_REPOS+=("validibot (community)")
        fi

        # Check commercial repos when they'll be included in the build
        if [ "${GCP_BUILD_VALIDIBOT_CLOUD:-0}" = "1" ]; then
            for REPO in validibot-pro validibot-cloud; do
                if [ -d "../$REPO" ] && [ -n "$(git -C "../$REPO" status --porcelain)" ]; then
                    DIRTY_REPOS+=("$REPO")
                fi
            done
        fi

        if [ ${#DIRTY_REPOS[@]} -gt 0 ]; then
            echo "Error: Uncommitted changes detected in:"
            for REPO in "${DIRTY_REPOS[@]}"; do
                echo "  - $REPO"
            done
            echo ""
            echo "Building from a dirty working tree means the image contents"
            echo "won't match the git SHA tag ({{git_sha}}). This makes rollbacks"
            echo "unreliable and debugging difficult."
            echo ""
            echo "Commit or stash your changes first, or set GCP_ALLOW_DIRTY=1"
            echo "to bypass this check in an emergency."
            exit 1
        fi
    else
        echo "Warning: GCP_ALLOW_DIRTY=1 — skipping dirty-repo check."
        echo "The image may contain uncommitted changes not tracked by git."
    fi

    echo "Building image: {{gcp_image}}:{{git_sha}}"

    DOCKERFILE="compose/production/django/Dockerfile"

    if [ "${GCP_BUILD_VALIDIBOT_CLOUD:-0}" = "1" ]; then
        # Copy Pro and Cloud repos into the build context (Docker can't
        # access parent directories). The cloud Dockerfile COPYs and
        # installs both packages.
        RSYNC_EXCLUDES="--exclude='.git' --exclude='.venv' --exclude='__pycache__' \
            --exclude='*.egg-info' --exclude='.pytest_cache' --exclude='.ruff_cache' \
            --exclude='.mypy_cache' --exclude='node_modules'"

        for REPO in validibot-pro validibot-cloud; do
            SRC="../$REPO"
            DST="_$REPO"

            if [ ! -d "$SRC" ]; then
                echo "Error: $SRC not found."
                echo "GCP_BUILD_VALIDIBOT_CLOUD=1 requires the $REPO repo as a sibling directory."
                exit 1
            fi

            echo "Copying $REPO into build context..."
            rsync -a $RSYNC_EXCLUDES "$SRC/" "$DST/"
        done

        cleanup() { rm -rf _validibot-pro _validibot-cloud; }
        trap cleanup EXIT

        # Use the cloud Dockerfile instead of the community one.
        DOCKERFILE="_validibot-cloud/deploy/Dockerfile"
    fi

    # --provenance=false: Docker 28+ defaults to provenance attestations,
    # which creates a multi-platform manifest index. When pushing a
    # cross-platform image (amd64 built on arm64 Mac), Artifact Registry
    # sees the manifest but can't resolve the platform, causing:
    #   "image was found but does not provide any platform"
    # Disabling provenance produces a simple single-platform image that
    # pushes and deploys correctly.
    #
    # Use --no-cache only when GCP_NO_CACHE=1 is set (e.g. after
    # Dockerfile RUN-line changes that Docker's layer cache would mask).
    # Default builds use the cache for speed — dependency installation
    # layers take 10+ minutes to rebuild from scratch.
    CACHE_FLAG=""
    if [ "${GCP_NO_CACHE:-0}" = "1" ]; then
        CACHE_FLAG="--no-cache"
        echo "Building with --no-cache (GCP_NO_CACHE=1)"
    fi
    docker build --platform linux/amd64 --provenance=false $CACHE_FLAG \
        -f "$DOCKERFILE" \
        -t {{gcp_image}}:{{git_sha}} \
        -t {{gcp_image}}:latest .

    if [ "${GCP_BUILD_VALIDIBOT_CLOUD:-0}" = "1" ]; then
        echo "Build complete. Commercial layers included (Pro + Cloud)."
    else
        echo "Build complete (community only)."
    fi

# Push Docker image to Google Artifact Registry
# Requires: gcloud auth configure-docker (run 'just gcp auth' first)
push:
    @echo "Pushing image: {{gcp_image}}:{{git_sha}}"
    docker push {{gcp_image}}:{{git_sha}}
    docker push {{gcp_image}}:latest

# =============================================================================
# Deploy Services
# =============================================================================

# Deploy web service to a specific stage.
# This is the main user-facing service that handles HTTP requests.
#
# The deploy chain runs in order:
#   1. build   — Build Docker image (with dirty-repo guard)
#   2. push    — Push to Artifact Registry
#   3. _migrate — Run database migrations via Cloud Run Job (new image)
#   4. _deploy-web — Update the Cloud Run service to the new image
#
# Migrations run BEFORE new instances start, so the database schema is
# always compatible with the code that's about to serve traffic. If
# migrations fail, the deploy is aborted and the current version keeps
# running.
#
# To skip migrations (e.g., hotfix with no schema changes):
#   GCP_SKIP_MIGRATE=1 just gcp deploy prod
#
# Key configuration:
#   - min-instances: prod=1 (avoid cold starts), dev/staging=0 (cost saving)
#   - max-instances: prod=4, dev/staging=2
#   - memory: 1Gi (sufficient for Django + gunicorn)
#   - allow-unauthenticated: public web access
[arg('stage', pattern='dev|staging|prod')]
deploy stage: _require-gcp-config (_require-not-maintenance stage) build push (_maybe-migrate stage) (_deploy-web stage)

# Deploy worker service to a specific stage.
# The worker handles background tasks triggered by Cloud Tasks.
#
# Key differences from web service:
#   - no-allow-unauthenticated: Only Cloud Tasks/Scheduler can invoke
#   - Always scales to zero (no warm instances needed)
#   - Same image as web, different APP_ROLE env var
#
# Migrations run before the worker deploy too, because the worker
# processes may rely on new database schema (new model fields, etc.).
#
# Usage: just gcp deploy-worker dev | just gcp deploy-worker prod
[arg('stage', pattern='dev|staging|prod')]
deploy-worker stage: _require-gcp-config (_require-not-maintenance stage) build push (_maybe-migrate stage) (_deploy-worker stage)

# Deploy both web and worker to a stage (single build+push, single migrate).
# Also creates/updates Cloud Scheduler jobs (idempotent) so that scheduled
# tasks like trial lifecycle emails are always in sync with the deployed code.
# Usage: just gcp deploy-all dev | just gcp deploy-all prod
[arg('stage', pattern='dev|staging|prod')]
deploy-all stage: _require-gcp-config (_require-not-maintenance stage) build push (_maybe-migrate stage) (_deploy-web stage) (_deploy-worker stage) (scheduler-setup stage)
    @echo ""
    @echo "All services deployed to {{stage}}"
    @echo ""
    @echo "Note: This deploy does NOT upload secrets. If you changed"
    @echo "environment variables in .envs/, run:"
    @echo "  just gcp secrets {{stage}}"
    @echo "  just gcp deploy-all {{stage}}"

# Internal: conditionally run migrations before deploy.
# Skipped when GCP_SKIP_MIGRATE=1 (for hotfixes with no schema changes).
[private]
_maybe-migrate stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "${GCP_SKIP_MIGRATE:-0}" = "1" ]; then
        echo ""
        echo "Skipping migrations (GCP_SKIP_MIGRATE=1)"
        echo "Warning: Ensure the new code is compatible with the current database schema."
        echo ""
    else
        just gcp _migrate "{{stage}}"
    fi

# Internal: deploy web service (assumes image already built and pushed)
_deploy-web stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Compute environment-specific names
    # Prod keeps 1 instance warm to avoid cold starts; dev/staging scale to zero
    APP_NAME="{{app_name}}"

    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-web"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
        MIN_INSTANCES=1
        MAX_INSTANCES=4
    else
        SERVICE="${APP_NAME}-web-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
        MIN_INSTANCES=0
        MAX_INSTANCES=2
    fi

    # Determine ingress mode based on whether a load balancer is configured.
    #
    # If a serverless NEG exists for this stage, the load balancer is set up
    # and we restrict ingress to internal + LB traffic only. This blocks
    # direct access to the *.run.app URL, forcing all public traffic through
    # the load balancer where Cloud Armor (WAF) rules can be applied.
    #
    # If no NEG exists (e.g., dev stage without a custom domain), fall back
    # to --ingress all so the service is still reachable via its *.run.app URL.
    if [ "{{stage}}" = "prod" ]; then NEG_NAME="${APP_NAME}-neg"; else NEG_NAME="${APP_NAME}-neg-{{stage}}"; fi
    if gcloud compute network-endpoint-groups describe "$NEG_NAME" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null 2>&1; then
        INGRESS="internal-and-cloud-load-balancing"
    else
        INGRESS="all"
    fi

    # Determine VPC egress mode. If the Cloud SQL instance has a private IP,
    # we enable Direct VPC egress so Cloud Run connects to the database over
    # the private network (never traversing the public internet). This uses
    # the default VPC and its default subnet — no VPC connector VMs needed.
    #
    # --vpc-egress=private-ranges-only means only traffic to private IP
    # ranges (10.x.x.x, etc.) routes through the VPC. Outbound traffic to
    # public IPs (Sentry, Stripe, PostHog) goes through the normal path.
    if [ "{{stage}}" = "prod" ]; then DB_INSTANCE="${APP_NAME}-db"; else DB_INSTANCE="${APP_NAME}-db-{{stage}}"; fi
    PRIVATE_IP=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project={{gcp_project}} \
        --format=json 2>/dev/null \
        | python3 -c "import json,sys; [print(a['ipAddress']) for a in json.load(sys.stdin).get('ipAddresses',[]) if a.get('type')=='PRIVATE']" 2>/dev/null || echo "")
    if [ -n "$PRIVATE_IP" ]; then
        VPC_FLAGS="--network=default --subnet=default --vpc-egress=private-ranges-only"
        echo "   Private IP detected ($PRIVATE_IP) — enabling Direct VPC egress"
    else
        VPC_FLAGS=""
        echo "   No private IP — skipping VPC egress (using Cloud SQL Auth Proxy)"
    fi

    echo "Deploying $SERVICE to Cloud Run ({{stage}}, ingress: $INGRESS)..."
    gcloud run deploy "$SERVICE" \
        --image {{gcp_image}}:{{git_sha}} \
        --region {{gcp_region}} \
        --port 8000 \
        --service-account "$SA" \
        --add-cloudsql-instances "$DB" \
        --set-secrets=/secrets/.env="$SECRET":latest \
        --set-env-vars APP_ROLE=web,VALIDIBOT_STAGE={{stage}} \
        --min-instances $MIN_INSTANCES \
        --max-instances $MAX_INSTANCES \
        --memory 1Gi \
        --timeout {{gcp_cloud_run_request_timeout}} \
        --execution-environment gen2 \
        --allow-unauthenticated \
        --ingress "$INGRESS" \
        --project {{gcp_project}} \
        $VPC_FLAGS

    echo ""
    echo "Web service deployed to {{stage}} (min-instances=$MIN_INSTANCES, ingress=$INGRESS)"

# Internal: deploy worker service (assumes image already built and pushed)
_deploy-worker stage:
    #!/usr/bin/env bash
    set -euo pipefail

    APP_NAME="{{app_name}}"

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-worker"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
    else
        SERVICE="${APP_NAME}-worker-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    # Same VPC egress detection as _deploy-web (see comments there).
    if [ "{{stage}}" = "prod" ]; then DB_INSTANCE="${APP_NAME}-db"; else DB_INSTANCE="${APP_NAME}-db-{{stage}}"; fi
    PRIVATE_IP=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project={{gcp_project}} \
        --format=json 2>/dev/null \
        | python3 -c "import json,sys; [print(a['ipAddress']) for a in json.load(sys.stdin).get('ipAddresses',[]) if a.get('type')=='PRIVATE']" 2>/dev/null || echo "")
    if [ -n "$PRIVATE_IP" ]; then
        VPC_FLAGS="--network=default --subnet=default --vpc-egress=private-ranges-only"
        echo "   Private IP detected ($PRIVATE_IP) — enabling Direct VPC egress"
    else
        VPC_FLAGS=""
    fi

    echo "Deploying $SERVICE to Cloud Run ({{stage}}, private)..."
    gcloud run deploy "$SERVICE" \
        --image {{gcp_image}}:{{git_sha}} \
        --region {{gcp_region}} \
        --port 8000 \
        --service-account "$SA" \
        --add-cloudsql-instances "$DB" \
        --set-secrets=/secrets/.env="$SECRET":latest \
        --set-env-vars APP_ROLE=worker,VALIDIBOT_STAGE={{stage}} \
        --no-allow-unauthenticated \
        --ingress internal \
        --min-instances 0 \
        --max-instances 2 \
        --memory 1Gi \
        --timeout {{gcp_cloud_run_request_timeout}} \
        --execution-environment gen2 \
        --project {{gcp_project}} \
        $VPC_FLAGS

    # Ensure Cloud Tasks queue is active so tasks are dispatched to the worker
    if [ "{{stage}}" = "prod" ]; then
        QUEUE_NAME="${APP_NAME}-tasks"
    else
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
    fi
    QUEUE_STATE=$(gcloud tasks queues describe "$QUEUE_NAME" \
        --location={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
    if [ "$QUEUE_STATE" = "PAUSED" ]; then
        echo "Resuming Cloud Tasks queue '$QUEUE_NAME'..."
        gcloud tasks queues resume "$QUEUE_NAME" \
            --location={{gcp_region}} \
            --project={{gcp_project}}
    fi

    echo ""
    echo "Worker service deployed to {{stage}} (ingress: internal)"

# =============================================================================
# Secrets Management
# =============================================================================

# Upload secrets for a specific stage
# Reads from local .envs/ directory and uploads to GCP Secret Manager.
#
# Usage: just gcp secrets dev | just gcp secrets prod
#
# Files:
#   - .envs/.dev/.google-cloud/.django -> django-env-dev
#   - .envs/.staging/.google-cloud/.django -> django-env-staging
#   - .envs/.production/.google-cloud/.django -> django-env
#
# After uploading, redeploy services to pick up new secrets.
[arg('stage', pattern='dev|staging|prod')]
secrets stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail

    # Compute secret name and source file
    if [ "{{stage}}" = "prod" ]; then
        SECRET_NAME="django-env"
        ENV_FILE=".envs/.production/.google-cloud/.django"
    else
        SECRET_NAME="django-env-{{stage}}"
        ENV_FILE=".envs/.{{stage}}/.google-cloud/.django"
    fi

    # Each stage must have its own env file with stage-specific values.
    # These are created manually — never copy from another stage, as
    # accidentally using production credentials in dev/staging could
    # cause data corruption or security issues.
    if [ ! -f "$ENV_FILE" ]; then
        echo "Error: $ENV_FILE not found"
        echo ""
        echo "Create the environment file manually:"
        echo "  mkdir -p $(dirname "$ENV_FILE")"
        echo "  # Create $ENV_FILE with stage-specific values"
        echo ""
        echo "See .envs.example/ for the required variables."
        echo "IMPORTANT: Do NOT copy from another stage's secrets."
        exit 1
    fi

    # Check if secret exists, create if not
    if ! gcloud secrets describe "$SECRET_NAME" --project={{gcp_project}} &>/dev/null; then
        echo "Creating new secret: $SECRET_NAME"
        gcloud secrets create "$SECRET_NAME" \
            --replication-policy="user-managed" \
            --locations="{{gcp_region}}" \
            --project={{gcp_project}}
    fi

    echo "Uploading secrets from $ENV_FILE to $SECRET_NAME..."
    gcloud secrets versions add "$SECRET_NAME" \
        --data-file="$ENV_FILE" \
        --project {{gcp_project}}

    echo ""
    echo "Secret $SECRET_NAME updated."
    echo "Run 'just gcp deploy {{stage}}' to apply changes."

# =============================================================================
# Operations
# =============================================================================

# View recent Cloud Run logs (last 50 entries)
# Usage: just gcp logs dev | just gcp logs prod
[arg('stage', pattern='dev|staging|prod')]
logs stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$SERVICE" \
        --project {{gcp_project}} \
        --limit 50 \
        --format="table(timestamp,severity,textPayload)"

# View logs and follow (stream new logs as they arrive)
# Usage: just gcp logs-follow dev | just gcp logs-follow prod
[arg('stage', pattern='dev|staging|prod')]
logs-follow stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    gcloud logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$SERVICE" \
        --project {{gcp_project}} \
        --format="table(timestamp,severity,textPayload)"

# Pause the service (block public access, but keep it deployed)
# Useful for maintenance or cost control without full teardown.
# Usage: just gcp pause dev | just gcp pause prod
[arg('stage', pattern='dev|staging|prod')]
pause stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    gcloud run services update "$SERVICE" \
        --region {{gcp_region}} \
        --ingress internal \
        --project {{gcp_project}}
    echo "$SERVICE paused. Public access blocked."

# Resume the service (restore public access)
# Uses internal-and-cloud-load-balancing if a load balancer is configured,
# otherwise falls back to --ingress all.
# Usage: just gcp resume dev | just gcp resume prod
[arg('stage', pattern='dev|staging|prod')]
resume stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi

    # Check if a load balancer NEG exists for this stage (indicates LB is set up)
    if [ "{{stage}}" = "prod" ]; then NEG_NAME="${APP_NAME}-neg"; else NEG_NAME="${APP_NAME}-neg-{{stage}}"; fi
    if gcloud compute network-endpoint-groups describe "$NEG_NAME" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null 2>&1; then
        INGRESS="internal-and-cloud-load-balancing"
    else
        INGRESS="all"
    fi

    gcloud run services update "$SERVICE" \
        --region {{gcp_region}} \
        --ingress "$INGRESS" \
        --project {{gcp_project}}
    echo "$SERVICE resumed (ingress: $INGRESS)"

# Show current service status and URL
# Usage: just gcp status dev | just gcp status prod
[arg('stage', pattern='dev|staging|prod')]
status stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
    fi

    echo "Web service:    $WEB_SERVICE"
    gcloud run services describe "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="table(status.url,spec.template.spec.timeoutSeconds,spec.template.spec.containerConcurrency)" 2>/dev/null || echo "  (not deployed)"
    echo ""
    echo "Worker service: $WORKER_SERVICE"
    gcloud run services describe "$WORKER_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="table(status.url,spec.template.spec.timeoutSeconds,spec.template.spec.containerConcurrency)" 2>/dev/null || echo "  (not deployed)"

    echo ""
    echo "Tip:"
    echo "  - SITE_URL should be your public base URL (prod: custom domain; dev/staging: web *.run.app URL is fine)."
    echo "  - WORKER_URL should be the worker *.run.app URL (used for callbacks and scheduled tasks)."

# Show status of all stages
status-all:
    @echo "=== DEV ===" && just gcp status dev 2>/dev/null || echo "(not deployed)"
    @echo ""
    @echo "=== STAGING ===" && just gcp status staging 2>/dev/null || echo "(not deployed)"
    @echo ""
    @echo "=== PROD ===" && just gcp status prod 2>/dev/null || echo "(not deployed)"

# Open the web service URL in browser
# Usage: just gcp open dev | just gcp open prod
[arg('stage', pattern='dev|staging|prod')]
open stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    URL=$(gcloud run services describe "$SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(status.url)" 2>/dev/null)
    if [ -z "$URL" ]; then
        echo "Error: Could not get URL for $SERVICE"
        exit 1
    fi
    echo "Opening $URL"
    open "$URL"

# Quick health check - just test if service is responding
# Usage: just gcp health-check dev | just gcp health-check prod
[arg('stage', pattern='dev|staging|prod')]
health-check stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    URL=$(gcloud run services describe "$SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(status.url)")
    echo "Checking: $URL"
    curl -s -o /dev/null -w "HTTP Status: %{http_code}\nTime: %{time_total}s\n" "$URL"

# =============================================================================
# Management Commands
# =============================================================================

# Run database migrations for a stage.
#
# This creates (or updates) a Cloud Run Job and executes it. The job uses
# the same image, service account, Cloud SQL instance, and secrets as the
# web service — so migrations run in an identical environment.
#
# When called standalone (`just gcp migrate prod`), the recipe looks up
# the image currently deployed on the web service. When called as part of
# a deploy (`just gcp deploy prod`), the internal `_migrate` recipe passes
# the newly-built image SHA so migrations run against the NEW code before
# any instances start serving traffic.
#
# The job is NOT deleted after execution — it stays around so you can
# inspect logs via `gcloud run jobs executions list` or the GCP console.
# It's updated in-place on subsequent runs.
#
# Usage:
#   just gcp migrate dev        # Run migrations using currently-deployed image
#   just gcp migrate prod       # Run migrations using currently-deployed image
[arg('stage', pattern='dev|staging|prod')]
migrate stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Determine which image to use: the one currently deployed on the web service.
    # (When called from the deploy chain, _migrate passes the newly-built image instead.)
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
    fi

    IMAGE=$(gcloud run services describe "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(spec.template.spec.containers[0].image)" 2>/dev/null) || {
        echo "Error: Could not determine deployed image for $WEB_SERVICE."
        echo "Is the web service deployed to {{stage}}?"
        exit 1
    }

    echo "Running migrations on {{stage}} with currently-deployed image..."
    echo "Image: $IMAGE"

    just gcp _run-migrate-job "{{stage}}" "$IMAGE"

# Internal: Run migrations as part of a deploy using the NEWLY-BUILT image.
#
# This is the key difference from the public `migrate` recipe: during a
# deploy, we want to run migrations with the NEW code (which may contain
# new migration files) BEFORE the new instances start serving traffic.
# Using the old image would miss any migrations added in this release.
#
# Called by: deploy, deploy-all, deploy-worker dependency chains
[private]
_migrate stage:
    #!/usr/bin/env bash
    set -euo pipefail

    echo "Running pre-deploy migrations on {{stage}} with new image..."
    echo "Image: {{gcp_image}}:{{git_sha}}"

    just gcp _run-migrate-job "{{stage}}" "{{gcp_image}}:{{git_sha}}"

# Internal: The actual Cloud Run Job creation and execution logic.
#
# Shared by both `migrate` (standalone) and `_migrate` (deploy chain).
# Separated to avoid duplicating the job orchestration code.
#
# How it works:
#   1. Compute stage-specific names (job, service account, DB, secret)
#   2. Create or update the Cloud Run Job with the given image
#   3. Execute the job and wait for it to complete
#   4. Check the exit status and fail the recipe if migrations failed
#   5. Show the execution logs for visibility
#
# The job runs: /entrypoint → gosu django → manage.py migrate --noinput
# followed by manage.py ensure_oidc_clients so the Claude public client stays
# registered without a manual admin step.
# This means it goes through the same entrypoint as the web service
# (loading /secrets/.env, waiting for the DB, etc.) but runs migrate
# instead of gunicorn.
[private]
_run-migrate-job stage image:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # ── Stage-specific configuration ────────────────────────────────────
    # Each stage has its own:
    #   - Job name: so concurrent deploys to different stages don't collide
    #   - Service account: for security isolation between environments
    #   - Cloud SQL instance: each stage has its own database
    #   - Secret: each stage has its own env vars in Secret Manager
    # ────────────────────────────────────────────────────────────────────
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="${APP_NAME}-migrate"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
    else
        JOB_NAME="${APP_NAME}-migrate-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Database Migration — {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Job:     $JOB_NAME"
    echo "  Image:   {{image}}"
    echo "  DB:      $DB"
    echo "  Account: $SA"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # ── Create or update the job ────────────────────────────────────────
    # Check if the job already exists. If so, update it with the new image.
    # If not, create it fresh. We use `update` rather than delete+create
    # so that previous execution history is preserved for debugging.
    # ────────────────────────────────────────────────────────────────────
    if gcloud run jobs describe "$JOB_NAME" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        echo "Updating Cloud Run Job '$JOB_NAME'..."
        gcloud run jobs update "$JOB_NAME" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --set-secrets=/secrets/.env="$SECRET":latest \
            --memory 1Gi \
            --task-timeout 300s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "-c,set -a && source /secrets/.env && set +a && python manage.py migrate --noinput && python manage.py createcachetable && python manage.py ensure_oidc_clients && echo Migrations, cache table, and OIDC client bootstrap complete" \
            --project {{gcp_project}} \
            --quiet
    else
        echo "Creating Cloud Run Job '$JOB_NAME'..."
        gcloud run jobs create "$JOB_NAME" \
            --image "{{image}}" \
            --region {{gcp_region}} \
            --service-account "$SA" \
            --set-cloudsql-instances "$DB" \
            --set-secrets=/secrets/.env="$SECRET":latest \
            --memory 1Gi \
            --task-timeout 300s \
            --execution-environment gen2 \
            --command "/bin/bash" \
            --args "-c,set -a && source /secrets/.env && set +a && python manage.py migrate --noinput && python manage.py createcachetable && python manage.py ensure_oidc_clients && echo Migrations, cache table, and OIDC client bootstrap complete" \
            --project {{gcp_project}} \
            --quiet
    fi

    # ── Execute and wait ────────────────────────────────────────────────
    # --wait blocks until the job finishes (success or failure).
    # We capture the execution name for log retrieval.
    # ────────────────────────────────────────────────────────────────────
    echo "Executing migrations (waiting for completion)..."
    EXECUTION=$(gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --wait \
        --format="value(metadata.name)" 2>&1) || {
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  ✗ MIGRATION FAILED on {{stage}}"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "  The deploy has been ABORTED. No new instances will be started."
        echo "  The currently-deployed version continues serving traffic."
        echo ""
        echo "  To investigate:"
        echo "    gcloud run jobs executions list --job=$JOB_NAME --region={{gcp_region}} --project={{gcp_project}}"
        echo "    gcloud beta run jobs executions logs read <execution-name> --region={{gcp_region}} --project={{gcp_project}}"
        echo ""
        echo "  Common causes:"
        echo "    - Migration file conflicts (two developers created migrations on the same app)"
        echo "    - Database schema already modified manually"
        echo "    - Missing data migration dependency"
        echo ""
        exit 1
    }

    # ── Show execution logs ─────────────────────────────────────────────
    # Non-fatal: the gcloud beta logs command can occasionally crash
    # even when the job succeeded. Don't fail the deploy for a log
    # retrieval issue.
    # ────────────────────────────────────────────────────────────────────
    echo ""
    echo "Execution: $EXECUTION"
    gcloud beta run jobs executions logs read "$EXECUTION" \
        --region {{gcp_region}} \
        --project {{gcp_project}} 2>/dev/null || echo "(Could not retrieve execution logs — check GCP console)"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ✓ Migrations complete on {{stage}}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Run setup_validibot to initialize site and default data
# Configures site domain, validators, roles, workspaces, etc.
# Usage: just gcp setup-data dev | just gcp setup-data prod
[arg('stage', pattern='dev|staging|prod')]
setup-data stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="${APP_NAME}-setup"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
    else
        JOB_NAME="${APP_NAME}-setup-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    echo "Running setup_validibot on {{stage}}..."

    # Use the image currently deployed on the web service (not :latest)
    if [ "{{stage}}" = "prod" ]; then WEB_SERVICE="${APP_NAME}-web"; else WEB_SERVICE="${APP_NAME}-web-{{stage}}"; fi
    DEPLOYED_IMAGE=$(gcloud run services describe "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(spec.template.spec.containers[0].image)" 2>/dev/null) || {
        echo "Error: Could not determine deployed image. Is the web service deployed?"
        echo "Falling back to :latest"
        DEPLOYED_IMAGE="{{gcp_image}}:latest"
    }
    echo "Using image: $DEPLOYED_IMAGE"

    # Delete existing job if present
    gcloud run jobs delete "$JOB_NAME" --region {{gcp_region}} --project {{gcp_project}} --quiet 2>/dev/null || true

    # Create and run job
    gcloud run jobs create "$JOB_NAME" \
        --image "$DEPLOYED_IMAGE" \
        --region {{gcp_region}} \
        --service-account "$SA" \
        --set-cloudsql-instances "$DB" \
        --set-secrets=/secrets/.env="$SECRET":latest \
        --memory 1Gi \
        --execution-environment gen2 \
        --command "/bin/bash" \
        --args "-c,set -a && source /secrets/.env && set +a && python manage.py setup_validibot --noinput" \
        --project {{gcp_project}}

    # Execute and wait for completion
    EXECUTION=$(gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --wait \
        --format="value(metadata.name)")

    echo "Execution: $EXECUTION"
    echo ""

    # Show logs from the completed execution (non-fatal: gcloud CLI can crash
    # on logs read even when the job succeeded - don't fail the recipe for that)
    gcloud beta run jobs executions logs read "$EXECUTION" \
        --region {{gcp_region}} \
        --project {{gcp_project}} || true

    echo ""
    echo "setup_validibot completed on {{stage}}"

# Run any Django management command on a deployed environment
# Creates a temporary Cloud Run Job, executes the command, then cleans up.
#
# Usage: just gcp management-cmd prod "seed_plans --force --skip-stripe"
# Usage: just gcp management-cmd dev "shell"
# Usage: just gcp management-cmd prod "migrate --check"
[arg('stage', pattern='dev|staging|prod')]
management-cmd stage command: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-web"
        SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
        SECRET="django-env"
    else
        SERVICE="${APP_NAME}-web-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    # Get current image from deployed service
    IMAGE=$(gcloud run services describe "$SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(spec.template.spec.containers[0].image)")

    if [ -z "$IMAGE" ]; then
        echo "Error: Could not get image from $SERVICE. Is it deployed?"
        exit 1
    fi

    # Generate unique job name
    JOB_NAME="manage-$(date +%s)"

    echo "Running: python manage.py {{command}}"
    echo "Stage: {{stage}}"
    echo "Image: $IMAGE"
    echo ""

    # Delete job if it exists (shouldn't with timestamp, but just in case)
    gcloud run jobs delete "$JOB_NAME" --region {{gcp_region}} --project {{gcp_project}} --quiet 2>/dev/null || true

    # Create the job
    gcloud run jobs create "$JOB_NAME" \
        --image "$IMAGE" \
        --region {{gcp_region}} \
        --service-account "$SA" \
        --set-cloudsql-instances "$DB" \
        --set-secrets=/secrets/.env="$SECRET":latest \
        --memory 1Gi \
        --execution-environment gen2 \
        --command "/bin/bash" \
        --args "-c,set -a && source /secrets/.env && set +a && python manage.py {{command}}" \
        --project {{gcp_project}}

    # Execute and wait for completion
    EXECUTION=$(gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --wait \
        --format="value(metadata.name)")

    echo "Execution: $EXECUTION"
    echo ""

    # Show logs from the completed execution (non-fatal: gcloud CLI can crash
    # on logs read even when the job succeeded - don't fail the recipe for that)
    gcloud beta run jobs executions logs read "$EXECUTION" \
        --region {{gcp_region}} \
        --project {{gcp_project}} || true

    # Clean up job after completion
    echo ""
    echo "Cleaning up job..."
    gcloud run jobs delete "$JOB_NAME" --region {{gcp_region}} --project {{gcp_project}} --quiet

    echo "Command completed on {{stage}}"

# View logs from a Cloud Run job execution
# Usage: just gcp job-logs {app_name}-migrate-dev
job-logs job:
    gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name={{job}}" \
        --project {{gcp_project}} \
        --limit 50 \
        --format="table(timestamp,textPayload)"

# =============================================================================
# Initial Stage Setup
# =============================================================================
#
# Use these commands to set up a new environment (dev, staging, prod) from scratch.
#
# Full setup workflow:
#   1. just gcp init-stage dev          # Create infrastructure (service account,
#                                       #   Cloud SQL, GCS storage bucket, Cloud
#                                       #   Tasks queue, KMS permissions, AUDIT
#                                       #   ARCHIVE bucket + CMEK + lifecycle)
#   2. Create .envs/.dev/.google-cloud/.django manually (see .envs.example/) —
#      include AUDIT_ARCHIVE_GCS_BUCKET and AUDIT_ARCHIVE_GCS_PROJECT_ID
#      printed at the end of init-stage
#   3. just gcp secrets dev             # Upload secrets to Secret Manager
#   4. just gcp deploy-all dev          # Build, migrate, and deploy services
#   5. just gcp setup-data dev          # Seed initial data
#
# =============================================================================

# Initialize infrastructure for a new stage (creates service account, database, etc.)
# Run this ONCE when setting up a new environment. Idempotent - safe to re-run.
#
# Usage: just gcp init-stage dev
[arg('stage', pattern='dev|staging|prod')]
init-stage stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"


    echo "============================================="
    echo "Initializing GCP infrastructure for: {{stage}}"
    echo "============================================="
    echo ""

    # Prod uses names without suffix, dev/staging use stage suffix
    if [ "{{stage}}" = "prod" ]; then
        SA_NAME="${APP_NAME}-cloudrun-prod"
        DB_INSTANCE="${APP_NAME}-db"
        SECRET_NAME="django-env"
        QUEUE_NAME="${APP_NAME}-tasks"
    else
        SA_NAME="${APP_NAME}-cloudrun-{{stage}}"
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
        SECRET_NAME="django-env-{{stage}}"
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
    fi
    SA_EMAIL="$SA_NAME@{{gcp_project}}.iam.gserviceaccount.com"

    # Step 1: Create service account
    echo "1. Creating service account: $SA_NAME"
    if gcloud iam service-accounts describe "$SA_EMAIL" --project={{gcp_project}} &>/dev/null; then
        echo "   Service account already exists"
    else
        gcloud iam service-accounts create "$SA_NAME" \
            --display-name="Validibot {{stage}} Cloud Run" \
            --project={{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # Step 2: Grant IAM roles to service account
    echo "2. Granting IAM roles to service account"
    ROLES=(
        "roles/cloudsql.client"
        "roles/secretmanager.secretAccessor"
        "roles/run.invoker"
        "roles/cloudtasks.enqueuer"
        "roles/cloudtasks.viewer"
        "projects/{{gcp_project}}/roles/${APP_NAME//-/_}_job_runner"
    )
    for role in "${ROLES[@]}"; do
        # Check if binding already exists
        if gcloud projects get-iam-policy {{gcp_project}} \
            --flatten="bindings[].members" \
            --filter="bindings.role=$role AND bindings.members=serviceAccount:$SA_EMAIL" \
            --format="value(bindings.role)" 2>/dev/null | grep -q .; then
            echo "   $role (already bound)"
        else
            gcloud projects add-iam-policy-binding {{gcp_project}} \
                --member="serviceAccount:$SA_EMAIL" \
                --role="$role" \
                --condition=None \
                --quiet &>/dev/null || true
            echo "   $role (added)"
        fi
    done
    echo ""

    # Step 2b: Grant SA-level permissions on itself (needed for Cloud Tasks OIDC tokens)
    echo "2b. Granting SA-level permissions (for Cloud Tasks OIDC)"
    SA_ROLES=(
        "roles/iam.serviceAccountTokenCreator"
        "roles/iam.serviceAccountUser"
    )
    for sa_role in "${SA_ROLES[@]}"; do
        role_short="${sa_role##*/}"
        if gcloud iam service-accounts get-iam-policy "$SA_EMAIL" --project={{gcp_project}} \
            --flatten="bindings[].members" \
            --filter="bindings.role=$sa_role AND bindings.members=serviceAccount:$SA_EMAIL" \
            --format="value(bindings.role)" 2>/dev/null | grep -q .; then
            echo "   $role_short (already bound)"
        else
            gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
                --member="serviceAccount:$SA_EMAIL" \
                --role="$sa_role" \
                --project={{gcp_project}} \
                --quiet &>/dev/null || true
            echo "   $role_short (added)"
        fi
    done
    echo ""

    # Step 2c: Create dedicated validator service account (least-privilege for Cloud Run Jobs)
    # Validators only need GCS access and run.invoker on the worker for callbacks.
    # They do NOT get secrets, Cloud SQL, Cloud Tasks, or KMS access.
    if [ "{{stage}}" = "prod" ]; then
        VALIDATOR_SA_NAME="${APP_NAME}-validator-prod"
    else
        VALIDATOR_SA_NAME="${APP_NAME}-validator-{{stage}}"
    fi
    VALIDATOR_SA_EMAIL="$VALIDATOR_SA_NAME@{{gcp_project}}.iam.gserviceaccount.com"

    echo "2c. Creating validator service account: $VALIDATOR_SA_NAME"
    if gcloud iam service-accounts describe "$VALIDATOR_SA_EMAIL" --project={{gcp_project}} &>/dev/null; then
        echo "   Validator service account already exists"
    else
        gcloud iam service-accounts create "$VALIDATOR_SA_NAME" \
            --display-name="Validibot {{stage}} Validator Jobs" \
            --project={{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # Step 3: Create Cloud SQL instance
    #
    # Tier selection:
    #   prod:    db-custom-2-8192 (2 vCPU, 8GB RAM) — dedicated cores for
    #            consistent performance under load, no noisy-neighbor effects.
    #   staging: db-g1-small (shared-core, 1.7GB RAM) — adequate for testing.
    #   dev:     db-f1-micro (shared-core, 0.6GB RAM) — minimal cost.
    #
    # Prod-only hardening:
    #   --availability-type=REGIONAL — automatic failover to a standby in
    #       another zone. Without this, a single zone outage takes the
    #       database offline.
    #   --deletion-protection — prevents accidental deletion via gcloud or
    #       the console. Must be explicitly disabled before deleting.
    #   --require-ssl — ensures all connections use TLS, even direct
    #       connections via authorized networks or gcloud sql connect.
    #       Cloud SQL Auth Proxy connections are already encrypted, but this
    #       closes the door on any unencrypted path.
    echo "3. Creating Cloud SQL instance: $DB_INSTANCE"
    if gcloud sql instances describe "$DB_INSTANCE" --project={{gcp_project}} &>/dev/null; then
        echo "   Database instance already exists"
    else
        if [ "{{stage}}" = "prod" ]; then
            TIER="db-custom-2-8192"
            EXTRA_FLAGS="--deletion-protection --require-ssl"
        elif [ "{{stage}}" = "staging" ]; then
            TIER="db-g1-small"
            EXTRA_FLAGS="--require-ssl"
        else
            TIER="db-f1-micro"
            EXTRA_FLAGS="--require-ssl"
        fi
        echo "   Creating $TIER instance (this may take several minutes)..."
        gcloud sql instances create "$DB_INSTANCE" \
            --database-version=POSTGRES_17 \
            --edition=ENTERPRISE \
            --tier="$TIER" \
            --region={{gcp_region}} \
            --storage-type=SSD \
            --storage-size=10GB \
            --storage-auto-increase \
            --backup \
            --project={{gcp_project}} \
            $EXTRA_FLAGS
        echo "   Created"
    fi
    echo ""

    # Step 3b: Enable private IP on Cloud SQL
    #
    # Private IP lets Cloud Run connect to the database over the VPC's
    # private network instead of the public internet. This requires:
    #   1. A private services access connection (VPC peering between your
    #      VPC and Google's services network). Created once per project.
    #   2. The Cloud SQL instance to have a private IP allocated.
    #
    # We keep the public IP as well so operators can connect via the
    # Cloud SQL Auth Proxy from any network. The public IP is protected
    # by --require-ssl and IAM authentication through the proxy.
    #
    # The deploy scripts auto-detect the private IP and enable Direct VPC
    # egress on Cloud Run when it's available.
    echo "3b. Enabling private IP on Cloud SQL"

    # Ensure the private services access connection exists (one-time per project).
    # This creates a VPC peering between the default VPC and Google's internal
    # services network, allocating a /20 IP range for private service IPs.
    PEERING_RANGE="${APP_NAME}-private-ip-range"
    if gcloud compute addresses describe "$PEERING_RANGE" --global --project={{gcp_project}} &>/dev/null 2>&1; then
        echo "   Private IP range already allocated"
    else
        echo "   Allocating private IP range for services..."
        gcloud compute addresses create "$PEERING_RANGE" \
            --global \
            --purpose=VPC_PEERING \
            --prefix-length=20 \
            --network=default \
            --project={{gcp_project}}
        echo "   Allocated"
    fi

    # Create the VPC peering connection (idempotent — errors if already exists)
    gcloud services vpc-peerings connect \
        --service=servicenetworking.googleapis.com \
        --ranges="$PEERING_RANGE" \
        --network=default \
        --project={{gcp_project}} 2>/dev/null || true

    # Enable private IP on the instance (keeps public IP too)
    CURRENT_PRIVATE=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project={{gcp_project}} \
        --format=json 2>/dev/null \
        | python3 -c "import json,sys; [print(a['ipAddress']) for a in json.load(sys.stdin).get('ipAddresses',[]) if a.get('type')=='PRIVATE']" 2>/dev/null || echo "")
    if [ -n "$CURRENT_PRIVATE" ]; then
        echo "   Private IP already enabled: $CURRENT_PRIVATE"
    else
        echo "   Enabling private IP on $DB_INSTANCE (this may take a few minutes)..."
        gcloud sql instances patch "$DB_INSTANCE" \
            --network=default \
            --project={{gcp_project}} \
            --quiet
        NEW_PRIVATE=$(gcloud sql instances describe "$DB_INSTANCE" \
            --project={{gcp_project}} \
            --format=json 2>/dev/null \
        | python3 -c "import json,sys; [print(a['ipAddress']) for a in json.load(sys.stdin).get('ipAddresses',[]) if a.get('type')=='PRIVATE']" 2>/dev/null || echo "")
        echo "   Private IP enabled: $NEW_PRIVATE"
        echo ""
        echo "   IMPORTANT: Update DATABASE_URL in your secrets to use this private IP:"
        echo "   postgres://validibot_user:PASSWORD@$NEW_PRIVATE:5432/validibot"
        echo ""
        echo "   Then run: just gcp secrets {{stage}}"
    fi
    echo ""

    # Step 4: Create database and user
    echo "4. Creating database and user"
    DB_NAME="validibot"
    DB_USER="validibot_user"

    # Check if database exists
    if gcloud sql databases describe "$DB_NAME" --instance="$DB_INSTANCE" --project={{gcp_project}} &>/dev/null; then
        echo "   Database '$DB_NAME' already exists"
    else
        gcloud sql databases create "$DB_NAME" \
            --instance="$DB_INSTANCE" \
            --project={{gcp_project}}
        echo "   Database created"
    fi

    # Check if user exists
    if gcloud sql users describe "$DB_USER" --instance="$DB_INSTANCE" --project={{gcp_project}} &>/dev/null; then
        echo "   User '$DB_USER' already exists"
    else
        # Generate random password
        DB_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)
        gcloud sql users create "$DB_USER" \
            --instance="$DB_INSTANCE" \
            --password="$DB_PASSWORD" \
            --project={{gcp_project}}
        echo "   User created"
        echo ""
        echo "   SAVE THIS PASSWORD - it won't be shown again:"
        echo "   Password: $DB_PASSWORD"
        echo ""
        echo "   DATABASE_URL for .envs/.{{stage}}/.django:"
        echo "   postgres://$DB_USER:$DB_PASSWORD@//$DB_NAME?host=/cloudsql/{{gcp_project}}:{{gcp_region}}:$DB_INSTANCE"
    fi
    echo ""

    # Step 5: Create Cloud Tasks queue
    echo "5. Creating Cloud Tasks queue"
    if gcloud tasks queues describe "$QUEUE_NAME" --location={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        echo "   Queue '$QUEUE_NAME' already exists"
    else
        gcloud tasks queues create "$QUEUE_NAME" \
            --location={{gcp_region}} \
            --project={{gcp_project}}
        echo "   Queue created"
    fi
    echo ""

    # Step 6: Create GCS bucket (single bucket with public/ and private/ prefixes)
    echo "6. Creating GCS bucket"
    if [ "{{stage}}" = "prod" ]; then
        STORAGE_BUCKET="${APP_NAME}-storage"
    else
        STORAGE_BUCKET="${APP_NAME}-storage-{{stage}}"
    fi

    if gcloud storage buckets describe "gs://$STORAGE_BUCKET" --project={{gcp_project}} &>/dev/null; then
        echo "   Bucket '$STORAGE_BUCKET' already exists"
    else
        gcloud storage buckets create "gs://$STORAGE_BUCKET" \
            --location={{gcp_region}} \
            --project={{gcp_project}} \
            --uniform-bucket-level-access
        echo "   Bucket '$STORAGE_BUCKET' created"
    fi

    # Grant objectAdmin role on the bucket (service account needs full access)
    gcloud storage buckets add-iam-policy-binding "gs://$STORAGE_BUCKET" \
        --member="serviceAccount:$SA_EMAIL" \
        --role="roles/storage.objectAdmin" \
        --project={{gcp_project}} \
        --quiet >/dev/null
    echo "   Granted objectAdmin on $STORAGE_BUCKET"

    # Make public/ prefix publicly readable via IAM Condition
    # This allows avatars, workflow images, etc. to be served directly
    echo "   Configuring public/ prefix for public read access..."
    gcloud storage buckets add-iam-policy-binding "gs://$STORAGE_BUCKET" \
        --member="allUsers" \
        --role="roles/storage.objectViewer" \
        --condition="expression=resource.name.startsWith(\"projects/_/buckets/$STORAGE_BUCKET/objects/public/\"),title=public-prefix-only" \
        --project={{gcp_project}} \
        --quiet >/dev/null 2>&1 || echo "   (public prefix IAM already configured)"
    echo "   Public prefix configured (public/ is publicly readable, private/ is protected)"

    # Grant validator SA storage access on the bucket (read inputs, write outputs)
    gcloud storage buckets add-iam-policy-binding "gs://$STORAGE_BUCKET" \
        --member="serviceAccount:$VALIDATOR_SA_EMAIL" \
        --role="roles/storage.objectAdmin" \
        --project={{gcp_project}} \
        --quiet >/dev/null
    echo "   Granted objectAdmin on $STORAGE_BUCKET to validator SA"
    echo ""

    # Step 7: Grant KMS permissions for credential signing
    echo "7. Granting KMS permissions"
    KMS_KEYRING="${APP_NAME}-keys"
    if [ "{{stage}}" = "prod" ]; then
        KMS_KEY="credential-signing"
    else
        KMS_KEY="credential-signing-{{stage}}"
    fi
    # Check if the key exists
    if gcloud kms keys describe "$KMS_KEY" --location={{gcp_region}} --keyring="$KMS_KEYRING" --project={{gcp_project}} &>/dev/null; then
        gcloud kms keys add-iam-policy-binding "$KMS_KEY" \
            --location={{gcp_region}} \
            --keyring="$KMS_KEYRING" \
            --project={{gcp_project}} \
            --member="serviceAccount:$SA_EMAIL" \
            --role=roles/cloudkms.viewer \
            --quiet &>/dev/null || true
        gcloud kms keys add-iam-policy-binding "$KMS_KEY" \
            --location={{gcp_region}} \
            --keyring="$KMS_KEYRING" \
            --project={{gcp_project}} \
            --member="serviceAccount:$SA_EMAIL" \
            --role=roles/cloudkms.signerVerifier \
            --quiet &>/dev/null || true
        echo "   KMS permissions granted for $KMS_KEY"
    else
        echo "   KMS key '$KMS_KEY' not found"
        echo "      Run 'just gcp kms-setup {{stage}}' first to create the signing key"
    fi
    echo ""

    # Step 8: Create secret placeholder
    echo "8. Creating secret in Secret Manager"
    if gcloud secrets describe "$SECRET_NAME" --project={{gcp_project}} &>/dev/null; then
        echo "   Secret '$SECRET_NAME' already exists"
    else
        echo "placeholder" | gcloud secrets create "$SECRET_NAME" \
            --replication-policy="user-managed" \
            --locations="{{gcp_region}}" \
            --data-file=- \
            --project={{gcp_project}}
        echo "   Secret created (placeholder)"
    fi
    echo ""

    # Step 9: Audit log archive bucket (CMEK-encrypted, lifecycle-tiered)
    #
    # Required by validibot_cloud.audit_archive.GCSArchiveBackend. A Django
    # system check at startup refuses to boot if AUDIT_ARCHIVE_GCS_BUCKET is
    # empty while that backend is selected, so this must exist before the
    # first cloud deploy.
    echo "9. Setting up audit archive bucket"
    just gcp audit-archive-setup {{stage}}
    if [ "{{stage}}" = "prod" ]; then
        AUDIT_BUCKET="{{gcp_project}}-${APP_NAME}-audit-archive"
    else
        AUDIT_BUCKET="{{gcp_project}}-${APP_NAME}-audit-archive-{{stage}}"
    fi
    echo ""

    # Summary
    echo "============================================="
    echo "Infrastructure setup complete for {{stage}}"
    echo "============================================="
    echo ""
    echo "Resources created:"
    echo "  Service account: $SA_EMAIL"
    echo "  Cloud SQL: $DB_INSTANCE (database: $DB_NAME)"
    echo "  Cloud Tasks queue: $QUEUE_NAME"
    echo "  GCS bucket: $STORAGE_BUCKET (public/ and private/ prefixes)"
    echo "  Audit archive bucket: $AUDIT_BUCKET (CMEK-encrypted, lifecycle-tiered)"
    echo "  KMS permissions: cloudkms.viewer, cloudkms.signerVerifier"
    echo "  Secret: $SECRET_NAME"
    echo ""
    if [ "{{stage}}" = "prod" ]; then
        ENV_PATH=".envs/.production/.google-cloud/.django"
    else
        ENV_PATH=".envs/.{{stage}}/.google-cloud/.django"
    fi

    echo "Next steps:"
    echo "  1. Configure env file:  Edit $ENV_PATH with:"
    echo "                          - STORAGE_BUCKET=$STORAGE_BUCKET"
    echo "                          - AUDIT_ARCHIVE_GCS_BUCKET=$AUDIT_BUCKET"
    echo "                          - AUDIT_ARCHIVE_GCS_PROJECT_ID={{gcp_project}}"
    echo "                          - POSTGRES_PASSWORD and DATABASE_URL (use password shown above)"
    echo "                          - DJANGO_SECRET_KEY (generate a new one)"
    echo "                          - DJANGO_ALLOWED_HOSTS (add Cloud Run URL after first deploy)"
    echo "  2. Upload secrets:      just gcp secrets {{stage}}"
    echo "  3. Deploy web service:  just gcp deploy {{stage}}  (runs migrations automatically)"
    echo "  4. Seed data:           just gcp setup-data {{stage}}"
    echo "  5. Set up scheduler:    just gcp scheduler-setup {{stage}}  (also runs as part of deploy-all)"
    echo "  6. Deploy validators:   just gcp validators-deploy-all {{stage}}  (optional)"
    echo ""

# List all resources for a stage (useful for verification)
[arg('stage', pattern='dev|staging|prod')]
list-resources stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    echo "Resources for stage: {{stage}}"
    echo "================================"
    echo ""

    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-web"
        WORKER="${APP_NAME}-worker"
        DB="${APP_NAME}-db"
        SECRET="django-env"
        SA="${APP_NAME}-cloudrun-prod"
        AUDIT_BUCKET="{{gcp_project}}-${APP_NAME}-audit-archive"
    else
        SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER="${APP_NAME}-worker-{{stage}}"
        DB="${APP_NAME}-db-{{stage}}"
        SECRET="django-env-{{stage}}"
        SA="${APP_NAME}-cloudrun-{{stage}}"
        AUDIT_BUCKET="{{gcp_project}}-${APP_NAME}-audit-archive-{{stage}}"
    fi

    echo "Cloud Run Services:"
    gcloud run services describe "$SERVICE" --region={{gcp_region}} --project={{gcp_project}} --format="value(status.url)" 2>/dev/null && echo "  $SERVICE" || echo "  $SERVICE (not deployed)"
    gcloud run services describe "$WORKER" --region={{gcp_region}} --project={{gcp_project}} --format="value(status.url)" 2>/dev/null && echo "  $WORKER" || echo "  $WORKER (not deployed)"
    echo ""

    echo "Cloud SQL:"
    gcloud sql instances describe "$DB" --project={{gcp_project}} --format="value(state)" 2>/dev/null && echo "  $DB" || echo "  $DB (not found)"
    echo ""

    echo "Audit Archive Bucket:"
    gcloud storage buckets describe "gs://$AUDIT_BUCKET" --project={{gcp_project}} &>/dev/null && echo "  $AUDIT_BUCKET" || echo "  $AUDIT_BUCKET (not found)"
    echo ""

    echo "Secrets:"
    gcloud secrets describe "$SECRET" --project={{gcp_project}} &>/dev/null && echo "  $SECRET" || echo "  $SECRET (not found)"
    echo ""

    echo "Service Account:"
    gcloud iam service-accounts describe "$SA@{{gcp_project}}.iam.gserviceaccount.com" --project={{gcp_project}} &>/dev/null && echo "  $SA" || echo "  $SA (not found)"

# =============================================================================
# Audit Log Archive (GCS bucket + CMEK + lifecycle)
# =============================================================================
#
# Provisions the bucket consumed by
# validibot_cloud.audit_archive.backends.gcs.GCSArchiveBackend. The backend
# writes JSONL+gzip partitions and a companion .sha256 sidecar per (org, day)
# under gs://<bucket>/audit/org_<id>/YYYY/MM/DD.jsonl.gz.
#
# A Django system check (validibot_cloud.audit_archive.E001) refuses to boot
# the app — including the migrate job — if AUDIT_ARCHIVE_GCS_BUCKET is empty
# while the GCS backend is selected. init-stage runs this recipe as Step 9,
# so fresh stages pick it up automatically. It is also exposed standalone so
# existing deployments (like prod today) can catch up without reprovisioning
# the rest of the stack.
#
# Layout mirrors the operations doc at
# ../validibot-cloud/docs/operations/audit-archive.md:
#
#   * Dedicated regional bucket, uniform IAM, versioning off
#   * CMEK key in {app_name}-keys keyring, key name "audit-archive",
#     30-day rotation
#   * Bucket default encryption set to that key
#   * Lifecycle: Nearline @ 30d, Coldline @ 180d, Archive @ 365d, NO delete
#   * Cloud Run SA gets objectCreator + objectViewer (viewer is needed for
#     verify-on-read), and cryptoKeyEncrypterDecrypter on the CMEK key
#
# Usage: just gcp audit-archive-setup dev|staging|prod
[arg('stage', pattern='dev|staging|prod')]
audit-archive-setup stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Resource naming follows the rest of this file: prod is unsuffixed,
    # non-prod stages carry the stage as a suffix. Prefixing the bucket
    # with the project ID guarantees global uniqueness — GCS bucket names
    # are a flat global namespace.
    if [ "{{stage}}" = "prod" ]; then
        SA_NAME="${APP_NAME}-cloudrun-prod"
        BUCKET="{{gcp_project}}-${APP_NAME}-audit-archive"
        KMS_KEY_NAME="audit-archive"
    else
        SA_NAME="${APP_NAME}-cloudrun-{{stage}}"
        BUCKET="{{gcp_project}}-${APP_NAME}-audit-archive-{{stage}}"
        KMS_KEY_NAME="audit-archive-{{stage}}"
    fi
    SA_EMAIL="$SA_NAME@{{gcp_project}}.iam.gserviceaccount.com"
    KMS_KEYRING="${APP_NAME}-keys"

    echo "============================================="
    echo "Audit archive setup for {{stage}}"
    echo "============================================="
    echo ""
    echo "Bucket:   gs://$BUCKET"
    echo "CMEK:     $KMS_KEYRING/$KMS_KEY_NAME"
    echo "SA:       $SA_EMAIL"
    echo ""

    # Step 1: Ensure the shared KMS keyring exists.
    # The keyring is reused across uses (credential signing lives here too);
    # creation is idempotent per keyring.
    echo "1. Ensuring KMS keyring: $KMS_KEYRING"
    if gcloud kms keyrings describe "$KMS_KEYRING" \
        --location={{gcp_region}} \
        --project={{gcp_project}} &>/dev/null; then
        echo "   Keyring already exists"
    else
        gcloud kms keyrings create "$KMS_KEYRING" \
            --location={{gcp_region}} \
            --project={{gcp_project}}
        echo "   Keyring created"
    fi
    echo ""

    # Step 2: Create the CMEK key. ENCRYPT_DECRYPT purpose (not signing).
    # 30-day rotation matches Google's recommendation for high-sensitivity
    # data. Software-level protection is fine for archive-tier workloads;
    # operators who need HSM can change --protection-level=hsm later.
    echo "2. Ensuring CMEK key: $KMS_KEY_NAME"
    if gcloud kms keys describe "$KMS_KEY_NAME" \
        --keyring="$KMS_KEYRING" \
        --location={{gcp_region}} \
        --project={{gcp_project}} &>/dev/null; then
        echo "   Key already exists"
    else
        gcloud kms keys create "$KMS_KEY_NAME" \
            --keyring="$KMS_KEYRING" \
            --location={{gcp_region}} \
            --purpose=encryption \
            --rotation-period=30d \
            --next-rotation-time="$(date -u -v+30d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+30 days' +%Y-%m-%dT%H:%M:%SZ)" \
            --project={{gcp_project}}
        echo "   Key created"
    fi
    KMS_KEY_RESOURCE="projects/{{gcp_project}}/locations/{{gcp_region}}/keyRings/$KMS_KEYRING/cryptoKeys/$KMS_KEY_NAME"
    echo ""

    # Step 3: Grant the GCS service agent permission to use the CMEK key.
    # Without this, bucket default-encryption on the CMEK fails at upload
    # time with a confusing 403, not at bucket-create time.
    #
    # Gotcha: the GCS service agent is created lazily by GCP on first use
    # of the Storage API in the project. If we just grant IAM directly,
    # gcloud errors with "Service account service-<num>@gs-project-accounts
    # ... does not exist" because the principal hasn't materialised yet.
    # The `--authorize-cmek` flag on `gcloud storage service-agent` both
    # materialises the agent AND grants cryptoKeyEncrypterDecrypter on the
    # supplied key in one call — designed specifically for this bootstrap
    # ordering problem.
    echo "3. Granting CMEK access to GCS service agent"
    gcloud storage service-agent \
        --project={{gcp_project}} \
        --authorize-cmek="$KMS_KEY_RESOURCE" \
        >/dev/null
    echo "   GCS service agent materialised and granted cryptoKeyEncrypterDecrypter"
    echo ""

    # Step 4: Create the bucket. Uniform bucket-level access (no ACLs),
    # versioning off (backend writes idempotently per (org, day), and
    # versions would just pile up). No public access whitelist.
    echo "4. Ensuring bucket: gs://$BUCKET"
    if gcloud storage buckets describe "gs://$BUCKET" --project={{gcp_project}} &>/dev/null; then
        echo "   Bucket already exists"
    else
        gcloud storage buckets create "gs://$BUCKET" \
            --location={{gcp_region}} \
            --project={{gcp_project}} \
            --uniform-bucket-level-access \
            --public-access-prevention \
            --default-storage-class=STANDARD
        echo "   Bucket created"
    fi
    echo ""

    # Step 5: Set bucket default encryption to the CMEK key. Existing
    # buckets get updated in place.
    echo "5. Setting bucket default encryption to CMEK"
    gcloud storage buckets update "gs://$BUCKET" \
        --default-encryption-key="$KMS_KEY_RESOURCE" \
        --project={{gcp_project}} \
        --quiet >/dev/null
    echo "   Default encryption set to $KMS_KEY_NAME"
    echo ""

    # Step 6: Disable versioning explicitly (create doesn't set it either
    # way; being explicit keeps the state obvious to audit).
    echo "6. Disabling object versioning"
    gcloud storage buckets update "gs://$BUCKET" \
        --no-versioning \
        --project={{gcp_project}} \
        --quiet >/dev/null
    echo "   Versioning off"
    echo ""

    # Step 7: Lifecycle rules — tier down to cheaper storage as data ages.
    # Deliberately NO delete action: audit archives are meant to outlive
    # Cloud SQL, and the regulatory horizon varies by jurisdiction.
    # Operators who need a delete rule add it separately.
    echo "7. Applying lifecycle rules (Nearline 30d / Coldline 180d / Archive 365d)"
    LIFECYCLE_JSON=$(cat <<'EOF'
    {
      "lifecycle": {
        "rule": [
          {
            "action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
            "condition": {"age": 30, "matchesStorageClass": ["STANDARD"]}
          },
          {
            "action": {"type": "SetStorageClass", "storageClass": "COLDLINE"},
            "condition": {"age": 180, "matchesStorageClass": ["NEARLINE"]}
          },
          {
            "action": {"type": "SetStorageClass", "storageClass": "ARCHIVE"},
            "condition": {"age": 365, "matchesStorageClass": ["COLDLINE"]}
          }
        ]
      }
    }
    EOF
    )
    TMP_LIFECYCLE=$(mktemp)
    trap "rm -f $TMP_LIFECYCLE" EXIT
    echo "$LIFECYCLE_JSON" > "$TMP_LIFECYCLE"
    gcloud storage buckets update "gs://$BUCKET" \
        --lifecycle-file="$TMP_LIFECYCLE" \
        --project={{gcp_project}} \
        --quiet >/dev/null
    echo "   Lifecycle applied"
    echo ""

    # Step 8: IAM on the bucket. The Cloud Run SA needs to write partitions
    # (objectCreator) and re-read them to verify the sha256 sidecar
    # (objectViewer). It deliberately does NOT get objectAdmin — audit
    # archives should be append-only from the app's perspective.
    if gcloud iam service-accounts describe "$SA_EMAIL" --project={{gcp_project}} &>/dev/null; then
        echo "8. Granting bucket IAM to Cloud Run SA"
        for role in roles/storage.objectCreator roles/storage.objectViewer; do
            gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
                --member="serviceAccount:$SA_EMAIL" \
                --role="$role" \
                --project={{gcp_project}} \
                --quiet >/dev/null
            echo "   Granted $role"
        done
        echo ""

        # Step 9: CMEK access for the Cloud Run SA. The backend can pass
        # kms_key_name on blob upload explicitly; that path also needs
        # encrypter/decrypter on the SA (not just the GCS service agent).
        echo "9. Granting CMEK access to Cloud Run SA"
        gcloud kms keys add-iam-policy-binding "$KMS_KEY_NAME" \
            --keyring="$KMS_KEYRING" \
            --location={{gcp_region}} \
            --project={{gcp_project}} \
            --member="serviceAccount:$SA_EMAIL" \
            --role=roles/cloudkms.cryptoKeyEncrypterDecrypter \
            --quiet &>/dev/null
        echo "   Granted cryptoKeyEncrypterDecrypter on $KMS_KEY_NAME"
    else
        echo "8-9. Skipping SA IAM: $SA_EMAIL does not exist yet"
        echo "     Run 'just gcp init-stage {{stage}}' first (or re-run this"
        echo "     after the SA is created)."
    fi
    echo ""

    echo "============================================="
    echo "Audit archive ready for {{stage}}"
    echo "============================================="
    echo ""
    echo "Add to your env file:"
    echo "  AUDIT_ARCHIVE_GCS_BUCKET=$BUCKET"
    echo "  AUDIT_ARCHIVE_GCS_PROJECT_ID={{gcp_project}}"
    echo "  # AUDIT_ARCHIVE_GCS_KMS_KEY=$KMS_KEY_RESOURCE  (optional; bucket default already set)"
    echo ""
    echo "Then: just gcp secrets {{stage}} && re-run the failing deploy."
    echo ""

# =============================================================================
# Cloud Scheduler
# =============================================================================
#
# Cloud Scheduler replaces Celery Beat for periodic tasks. Each job calls an
# HTTP endpoint on the worker service using OIDC authentication.
#
# Prerequisites:
#   - Worker service deployed ({app_name}-worker)
#   - Cloud Scheduler API enabled: gcloud services enable cloudscheduler.googleapis.com
#   - Service account with Cloud Run Invoker role
#
# Jobs are configured in Australia/Sydney timezone by default.
#
# =============================================================================

# List all Cloud Scheduler jobs for this project
scheduler-list:
    gcloud scheduler jobs list \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# Set up all scheduled jobs for a stage (dev, staging, prod)
# Creates jobs for session cleanup, idempotency key cleanup, callback receipts,
# submission purging, and stuck run cleanup.
[arg('stage', pattern='dev|staging|prod')]
scheduler-setup stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute stage-specific values
    if [ "{{stage}}" = "prod" ]; then
        WORKER_SERVICE="${APP_NAME}-worker"
        SCHEDULER_SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        JOB_SUFFIX=""
    else
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        SCHEDULER_SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "Setting up Cloud Scheduler jobs for {{stage}} environment..."
    echo "Worker service: $WORKER_SERVICE"
    echo "Service account: $SCHEDULER_SA"
    echo ""

    # Get the worker service URL
    WORKER_URL=$(gcloud run services describe "$WORKER_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(status.url)" 2>/dev/null || echo "")

    if [ -z "$WORKER_URL" ]; then
        echo "ERROR: Worker service $WORKER_SERVICE not found."
        echo "Deploy the worker service first with: just gcp deploy-worker {{stage}}"
        exit 1
    fi

    echo "Worker URL: $WORKER_URL"
    echo ""

    # Helper function to create or update a scheduler job
    create_or_update_job() {
        local job_name=$1
        local schedule=$2
        local endpoint=$3
        local description=$4

        echo "Setting up: $job_name"
        echo "   Schedule: $schedule"
        echo "   Endpoint: $endpoint"

        if gcloud scheduler jobs describe "$job_name" \
            --project {{gcp_project}} \
            --location {{gcp_region}} &>/dev/null; then
            echo "   Updating existing job..."
            gcloud scheduler jobs update http "$job_name" \
                --project {{gcp_project}} \
                --location {{gcp_region}} \
                --schedule "$schedule" \
                --time-zone "{{gcp_scheduler_timezone}}" \
                --uri "${WORKER_URL}${endpoint}" \
                --http-method POST \
                --oidc-service-account-email "$SCHEDULER_SA" \
                --oidc-token-audience "$WORKER_URL" \
                --description "$description"
        else
            echo "   Creating new job..."
            gcloud scheduler jobs create http "$job_name" \
                --project {{gcp_project}} \
                --location {{gcp_region}} \
                --schedule "$schedule" \
                --time-zone "{{gcp_scheduler_timezone}}" \
                --uri "${WORKER_URL}${endpoint}" \
                --http-method POST \
                --oidc-service-account-email "$SCHEDULER_SA" \
                --oidc-token-audience "$WORKER_URL" \
                --description "$description"
        fi
        echo "   Done"
        echo ""
    }

    # Job 1: Clear expired sessions (daily at 2 AM)
    create_or_update_job \
        "${APP_NAME}-clear-sessions${JOB_SUFFIX}" \
        "0 2 * * *" \
        "/api/v1/scheduled/clear-sessions/" \
        "Clear expired Django sessions ({{stage}})"

    # Job 2: Cleanup idempotency keys (daily at 3 AM)
    create_or_update_job \
        "${APP_NAME}-cleanup-idempotency-keys${JOB_SUFFIX}" \
        "0 3 * * *" \
        "/api/v1/scheduled/cleanup-idempotency-keys/" \
        "Delete expired API idempotency keys - 24h TTL ({{stage}})"

    # Job 3: Cleanup callback receipts (weekly Sunday at 4 AM)
    create_or_update_job \
        "${APP_NAME}-cleanup-callback-receipts${JOB_SUFFIX}" \
        "0 4 * * 0" \
        "/api/v1/scheduled/cleanup-callback-receipts/" \
        "Delete old validator callback receipts - 30 day retention ({{stage}})"

    # Job 4: Purge expired submissions (hourly at :00)
    create_or_update_job \
        "${APP_NAME}-purge-expired-submissions${JOB_SUFFIX}" \
        "0 * * * *" \
        "/api/v1/scheduled/purge-expired-submissions/" \
        "Purge submission content past retention period ({{stage}})"

    # Job 5: Process purge retries (every 5 minutes)
    create_or_update_job \
        "${APP_NAME}-process-purge-retries${JOB_SUFFIX}" \
        "*/5 * * * *" \
        "/api/v1/scheduled/process-purge-retries/" \
        "Retry failed submission purges ({{stage}})"

    # Job 6: Cleanup stuck runs (every 10 minutes)
    create_or_update_job \
        "${APP_NAME}-cleanup-stuck-runs${JOB_SUFFIX}" \
        "*/10 * * * *" \
        "/api/v1/scheduled/cleanup-stuck-runs/" \
        "Mark stuck validation runs as FAILED - 30min timeout ({{stage}})"

    # Job 7: Send periodic emails (every 6 hours)
    create_or_update_job \
        "${APP_NAME}-send-periodic-emails${JOB_SUFFIX}" \
        "0 */6 * * *" \
        "/api/v1/scheduled/send-periodic-emails/" \
        "Run periodic email handlers - trial lifecycle etc ({{stage}})"

    echo "All scheduler jobs configured for {{stage}}!"
    echo ""
    echo "View jobs: just gcp scheduler-list"
    echo "Run a job manually: just gcp scheduler-run <job-name>"

# Run a scheduler job manually (useful for testing)
scheduler-run job_name:
    gcloud scheduler jobs run {{job_name}} \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# Delete all scheduler jobs for a stage (use with caution)
[arg('stage', pattern='dev|staging|prod')]
scheduler-delete-all stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        JOB_SUFFIX=""
    else
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "This will delete ALL scheduler jobs for {{stage}} environment"
    read -p "Are you sure? (y/N) " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs; do
            job="${job_base}${JOB_SUFFIX}"
            echo "Deleting $job..."
            gcloud scheduler jobs delete "$job" \
                --project {{gcp_project}} \
                --location {{gcp_region}} \
                --quiet || echo "  (job not found)"
        done
        echo "Done."
    else
        echo "Cancelled."
    fi

# Pause all scheduler jobs for a stage
[arg('stage', pattern='dev|staging|prod')]
scheduler-pause-all stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        JOB_SUFFIX=""
    else
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "Pausing all scheduler jobs for {{stage}}..."
    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs; do
        job="${job_base}${JOB_SUFFIX}"
        echo "Pausing $job..."
        gcloud scheduler jobs pause "$job" \
            --project {{gcp_project}} \
            --location {{gcp_region}} \
            --quiet 2>/dev/null || echo "  (job not found or already paused)"
    done
    echo "All scheduler jobs paused for {{stage}}"

# Resume all scheduler jobs for a stage
[arg('stage', pattern='dev|staging|prod')]
scheduler-resume-all stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        JOB_SUFFIX=""
    else
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "Resuming all scheduler jobs for {{stage}}..."
    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs; do
        job="${job_base}${JOB_SUFFIX}"
        echo "Resuming $job..."
        gcloud scheduler jobs resume "$job" \
            --project {{gcp_project}} \
            --location {{gcp_region}} \
            --quiet 2>/dev/null || echo "  (job not found or already running)"
    done
    echo "All scheduler jobs resumed for {{stage}}"

# Pause a single scheduler job
scheduler-pause job_name:
    gcloud scheduler jobs pause {{job_name}} \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# Resume a paused scheduler job
scheduler-resume job_name:
    gcloud scheduler jobs resume {{job_name}} \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# =============================================================================
# Validator Containers (Cloud Run Jobs)
# =============================================================================
#
# Validators run as separate Docker containers (Cloud Run Jobs) that are
# invoked by the worker service. Each validator type has its own image.
#
# Current validators:
#   - energyplus: EnergyPlus simulation validator
#   - fmu: Functional Mock-up Unit validator
#
# Validators live in the separate validibot-validators repository at
# ../validibot-validators. Run these commands from that directory.
#
# =============================================================================

# Build a specific validator container (energyplus, fmu, etc.)
# Usage: just gcp validator-build energyplus
# NOTE: Run from ../validibot-validators directory
validator-build name:
    docker build --platform linux/amd64 \
        -f ../validibot-validators/validators/{{name}}/Dockerfile \
        -t {{validator_repo}}/{{app_name}}-validator-{{name}}:{{git_sha}} \
        -t {{validator_repo}}/{{app_name}}-validator-{{name}}:latest \
        ../validibot-validators

# Push a validator container
validator-push name:
    docker push {{validator_repo}}/{{app_name}}-validator-{{name}}:{{git_sha}}
    docker push {{validator_repo}}/{{app_name}}-validator-{{name}}:latest

# Build and push in one step
validator-build-push name: (validator-build name) (validator-push name)

# Deploy a Cloud Run Job for a validator to a specific stage
# Usage: just gcp validator-deploy energyplus dev | just gcp validator-deploy fmu prod
[arg('stage', pattern='dev|staging|prod')]
validator-deploy name stage: _require-gcp-config (validator-build-push name)
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    # Compute stage-specific names
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="${APP_NAME}-validator-{{name}}"
        MAIN_SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        VALIDATOR_SA="${APP_NAME}-validator-prod@{{gcp_project}}.iam.gserviceaccount.com"
        WORKER_SERVICE="${APP_NAME}-worker"
    else
        JOB_NAME="${APP_NAME}-validator-{{name}}-{{stage}}"
        MAIN_SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        VALIDATOR_SA="${APP_NAME}-validator-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
    fi

    # Deploy the job with the dedicated validator SA (least-privilege)
    echo "Deploying $JOB_NAME..."
    gcloud run jobs deploy "$JOB_NAME" \
        --image {{validator_repo}}/{{app_name}}-validator-{{name}}:{{git_sha}} \
        --region {{gcp_region}} \
        --service-account "$VALIDATOR_SA" \
        --max-retries 0 \
        --task-timeout 3600 \
        --execution-environment gen2 \
        --network default \
        --subnet default \
        --vpc-egress all-traffic \
        --set-env-vars VALIDATOR_VERSION={{git_sha}},VALIDIBOT_STAGE={{stage}},DEPLOYMENT_TARGET=gcp \
        --labels validator={{name}},version={{git_sha}},stage={{stage}} \
        --project {{gcp_project}}
    echo "$JOB_NAME deployed"

    # Grant the main SA (web/worker) permission to trigger this job with overrides
    echo "Granting job runner permission to main SA on $JOB_NAME..."
    gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --member="serviceAccount:$MAIN_SA" \
        --role="projects/{{gcp_project}}/roles/${APP_NAME//-/_}_job_runner"
    echo "   Main SA can trigger $JOB_NAME"

    # Grant the validator SA permission to call the worker service (for callbacks)
    echo "Granting run.invoker on $WORKER_SERVICE to validator SA..."
    if gcloud run services add-iam-policy-binding "$WORKER_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --member="serviceAccount:$VALIDATOR_SA" \
        --role="roles/run.invoker" \
        --quiet >/dev/null 2>&1; then
        echo "   Validator SA can invoke $WORKER_SERVICE"
    else
        echo "   WARNING: Worker service $WORKER_SERVICE not found yet."
        echo "   Deploy the worker first (just gcp deploy-worker {{stage}}), then re-run validator-deploy."
    fi

# Build and deploy all validator jobs to a stage
# Usage: just gcp validators-deploy-all dev | just gcp validators-deploy-all prod
[arg('stage', pattern='dev|staging|prod')]
validators-deploy-all stage: _require-gcp-config
    just gcp validator-deploy energyplus {{stage}}
    just gcp validator-deploy fmu {{stage}}

# =============================================================================
# Validator Assets Management
# =============================================================================
#
# Validator assets (weather files, etc.) are stored in GCS under:
#   gs://{bucket}/validator_assets/{asset_type}/
#
# Weather data files go in the weather_data subdirectory.
# Each environment has its own bucket.
#
# =============================================================================

# Sync weather data files from local ../weather_data directory to GCS
# Usage: just gcp sync-weather dev|staging|prod|all
sync-weather env:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    WEATHER_SOURCE="../weather_data"
    ASSETS_PREFIX="validator_assets/weather_data"

    if [ ! -d "$WEATHER_SOURCE" ]; then
        echo "Error: Weather data directory not found: $WEATHER_SOURCE"
        echo "Please create the directory and add EPW files to sync."
        exit 1
    fi

    EPW_COUNT=$(find "$WEATHER_SOURCE" -name "*.epw" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$EPW_COUNT" -eq 0 ]; then
        echo "Error: No EPW files found in $WEATHER_SOURCE"
        exit 1
    fi
    echo "Found $EPW_COUNT EPW file(s) to sync"

    sync_to_bucket() {
        local bucket=$1
        local env_name=$2
        local gcs_path="gs://$bucket/$ASSETS_PREFIX/"
        echo ""
        echo "Syncing to $env_name bucket: $gcs_path"

        if ! gsutil -m rsync -r "$WEATHER_SOURCE/" "$gcs_path"; then
            echo "ERROR: gsutil rsync failed for $env_name"
            exit 1
        fi

        local remote_count
        remote_count=$(gsutil ls "$gcs_path*.epw" 2>/dev/null | wc -l | tr -d ' ')

        if [ "$remote_count" -eq 0 ]; then
            echo "ERROR: No EPW files found in bucket after sync"
            exit 1
        fi

        if [ "$remote_count" -ne "$EPW_COUNT" ]; then
            echo "WARNING: Local has $EPW_COUNT files, bucket has $remote_count files"
        fi

        echo "$env_name sync complete ($remote_count files in bucket)"
    }

    case "{{env}}" in
        dev)
            sync_to_bucket "${APP_NAME}-storage-dev" "dev"
            ;;
        staging)
            sync_to_bucket "${APP_NAME}-storage-staging" "staging"
            ;;
        prod)
            echo "WARNING: You are about to sync weather files to PRODUCTION"
            read -p "Are you sure? (yes/no): " confirm
            if [ "$confirm" != "yes" ]; then
                echo "Aborted."
                exit 1
            fi
            sync_to_bucket "${APP_NAME}-storage" "prod"
            ;;
        all)
            sync_to_bucket "${APP_NAME}-storage-dev" "dev"
            sync_to_bucket "${APP_NAME}-storage-staging" "staging"
            echo ""
            echo "WARNING: About to sync to PRODUCTION"
            read -p "Continue with prod sync? (yes/no): " confirm
            if [ "$confirm" == "yes" ]; then
                sync_to_bucket "${APP_NAME}-storage" "prod"
            else
                echo "Skipped prod sync."
            fi
            ;;
        *)
            echo "Error: Invalid environment '{{env}}'"
            echo "Usage: just gcp sync-weather dev|staging|prod|all"
            exit 1
            ;;
    esac

    echo ""
    echo "Weather data sync complete"

# List weather files in a bucket
# Usage: just gcp list-weather dev|staging|prod
list-weather env:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    ASSETS_PREFIX="validator_assets/weather_data"

    case "{{env}}" in
        dev)
            BUCKET="${APP_NAME}-storage-dev"
            ;;
        staging)
            BUCKET="${APP_NAME}-storage-staging"
            ;;
        prod)
            BUCKET="${APP_NAME}-storage"
            ;;
        *)
            echo "Error: Invalid environment '{{env}}'"
            echo "Usage: just gcp list-weather dev|staging|prod"
            exit 1
            ;;
    esac

    echo "Weather files in gs://$BUCKET/$ASSETS_PREFIX/:"
    gsutil ls -l "gs://$BUCKET/$ASSETS_PREFIX/" 2>/dev/null || echo "  (no files found)"

# =============================================================================
# Maintenance Mode
# =============================================================================
#
# Put a stage into maintenance mode to save costs when not in use.
# This pauses/scales down resources without deleting them.
#
# What gets paused:
#   - Cloud Run services: Ingress set to 'internal' (blocks public traffic)
#   - Cloud SQL: Instance stopped (takes ~1 min to restart)
#   - Cloud Scheduler: All jobs paused
#   - Cloud Tasks queue: Paused
#
# Note: Cloud Run will still scale to 0 when idle, but maintenance mode
# ensures no traffic can reach the services and stops the database.
#
# =============================================================================

# Put a stage into maintenance mode (pause all resources)
# Usage: just gcp maintenance-on dev
[arg('stage', pattern='dev|staging|prod')]
maintenance-on stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Safety check for prod
    if [ "{{stage}}" = "prod" ]; then
        echo "WARNING: You are about to put PRODUCTION into maintenance mode!"
        echo "This will make the site unavailable to all users."
        read -p "Are you absolutely sure? Type 'yes' to confirm: " -r
        if [[ ! "$REPLY" == "yes" ]]; then
            echo "Cancelled."
            exit 1
        fi
    fi

    # Compute stage-specific names
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
        DB_INSTANCE="${APP_NAME}-db"
        QUEUE_NAME="${APP_NAME}-tasks"
        JOB_SUFFIX=""
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "Putting {{stage}} into maintenance mode..."
    echo ""

    # 1. Block public traffic to web service
    echo "1. Blocking public traffic to $WEB_SERVICE..."
    gcloud run services update "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --ingress internal \
        --project {{gcp_project}} \
        --quiet
    echo "   Web service set to internal-only"

    # 2. Worker is already internal-only, but ensure it's set
    echo "2. Confirming $WORKER_SERVICE is internal-only..."
    gcloud run services update "$WORKER_SERVICE" \
        --region {{gcp_region}} \
        --ingress internal \
        --project {{gcp_project}} \
        --quiet 2>/dev/null || echo "   (worker not found or already internal)"
    echo "   Worker service confirmed internal-only"

    # 3. Pause Cloud Scheduler jobs
    echo "3. Pausing Cloud Scheduler jobs..."
    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs; do
        job="${job_base}${JOB_SUFFIX}"
        gcloud scheduler jobs pause "$job" \
            --project {{gcp_project}} \
            --location {{gcp_region}} \
            --quiet 2>/dev/null && echo "   Paused $job" || echo "   - $job (not found)"
    done

    # 4. Pause Cloud Tasks queue
    echo "4. Pausing Cloud Tasks queue..."
    gcloud tasks queues pause "$QUEUE_NAME" \
        --location {{gcp_region}} \
        --project {{gcp_project}} \
        --quiet 2>/dev/null && echo "   Queue paused" || echo "   - Queue not found"

    # 5. Stop Cloud SQL instance
    echo "5. Stopping Cloud SQL instance $DB_INSTANCE..."
    echo "   (This saves the most cost but takes ~1 min to restart)"
    DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "unknown")
    if [ "$DB_STATUS" = "RUNNABLE" ]; then
        gcloud sql instances patch "$DB_INSTANCE" \
            --activation-policy NEVER \
            --project {{gcp_project}} \
            --quiet
        echo "   Database stopped"
    else
        echo "   - Database already stopped ($DB_STATUS)"
    fi

    echo ""
    echo "{{stage}} is now in MAINTENANCE MODE"
    echo ""
    echo "Resources paused (not deleted):"
    echo "  Web service: internal-only (no public traffic)"
    echo "  Worker service: internal-only"
    echo "  Scheduler jobs: paused"
    echo "  Task queue: paused"
    echo "  Database: stopped"
    echo ""
    echo "To bring back online: just gcp maintenance-off {{stage}}"

# Bring a stage out of maintenance mode (resume all resources)
# Usage: just gcp maintenance-off dev
[arg('stage', pattern='dev|staging|prod')]
maintenance-off stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Compute stage-specific names
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
        DB_INSTANCE="${APP_NAME}-db"
        QUEUE_NAME="${APP_NAME}-tasks"
        JOB_SUFFIX=""
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "Bringing {{stage}} out of maintenance mode..."
    echo ""

    # 1. Start Cloud SQL instance first (takes longest)
    echo "1. Starting Cloud SQL instance $DB_INSTANCE..."
    echo "   (This may take 1-2 minutes)"
    DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "unknown")
    if [ "$DB_STATUS" = "RUNNABLE" ]; then
        echo "   - Database already running ($DB_STATUS)"
    else
        gcloud sql instances patch "$DB_INSTANCE" \
            --activation-policy ALWAYS \
            --project {{gcp_project}} \
            --quiet
        echo "   Database starting..."
    fi

    # 2. Resume Cloud Tasks queue
    echo "2. Resuming Cloud Tasks queue..."
    gcloud tasks queues resume "$QUEUE_NAME" \
        --location {{gcp_region}} \
        --project {{gcp_project}} \
        --quiet 2>/dev/null && echo "   Queue resumed" || echo "   - Queue not found"

    # 3. Resume Cloud Scheduler jobs
    echo "3. Resuming Cloud Scheduler jobs..."
    for job_base in ${APP_NAME}-clear-sessions ${APP_NAME}-cleanup-idempotency-keys ${APP_NAME}-cleanup-callback-receipts ${APP_NAME}-purge-expired-submissions ${APP_NAME}-process-purge-retries ${APP_NAME}-cleanup-stuck-runs; do
        job="${job_base}${JOB_SUFFIX}"
        gcloud scheduler jobs resume "$job" \
            --project {{gcp_project}} \
            --location {{gcp_region}} \
            --quiet 2>/dev/null && echo "   Resumed $job" || echo "   - $job (not found)"
    done

    # 4. Restore public traffic to web service
    # Use internal-and-cloud-load-balancing if LB is set up, otherwise fall back to all
    echo "4. Restoring public traffic to $WEB_SERVICE..."
    if [ "{{stage}}" = "prod" ]; then NEG_NAME="${APP_NAME}-neg"; else NEG_NAME="${APP_NAME}-neg-{{stage}}"; fi
    if gcloud compute network-endpoint-groups describe "$NEG_NAME" --region={{gcp_region}} --project={{gcp_project}} &>/dev/null 2>&1; then
        INGRESS="internal-and-cloud-load-balancing"
    else
        INGRESS="all"
    fi
    gcloud run services update "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --ingress "$INGRESS" \
        --project {{gcp_project}} \
        --quiet
    echo "   Web service accepting traffic (ingress: $INGRESS)"

    # 5. Worker stays internal-only (that's correct)
    echo "5. Worker service remains internal-only (correct configuration)"

    # Wait for database to be ready
    echo ""
    echo "6. Waiting for database to be ready..."
    for i in {1..30}; do
        STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
            --project {{gcp_project}} \
            --format="value(state)" 2>/dev/null)
        if [ "$STATUS" = "RUNNABLE" ]; then
            echo "   Database is ready!"
            break
        fi
        echo "   Waiting... ($STATUS)"
        sleep 5
    done

    echo ""
    echo "{{stage}} is now ONLINE"
    echo ""
    echo "All resources resumed:"
    echo "  Web service: accepting public traffic"
    echo "  Worker service: internal-only (correct)"
    echo "  Scheduler jobs: running"
    echo "  Task queue: running"
    echo "  Database: running"
    echo ""
    echo "Check status: just gcp status {{stage}}"
    echo "Open site: just gcp open {{stage}}"

# Check maintenance status for a stage
[arg('stage', pattern='dev|staging|prod')]
maintenance-status stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    # Compute stage-specific names
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        DB_INSTANCE="${APP_NAME}-db"
        QUEUE_NAME="${APP_NAME}-tasks"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
        QUEUE_NAME="${APP_NAME}-validation-queue-{{stage}}"
    fi

    echo "Maintenance status for {{stage}}:"
    echo "=================================="
    echo ""

    # Check web service ingress
    INGRESS=$(gcloud run services describe "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(spec.template.metadata.annotations.'run.googleapis.com/ingress')" 2>/dev/null || echo "unknown")
    if [ "$INGRESS" = "all" ]; then
        echo "Web service:  ONLINE (public traffic allowed)"
    else
        echo "Web service:  MAINTENANCE ($INGRESS)"
    fi

    # Check database status
    DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "unknown")
    if [ "$DB_STATUS" = "RUNNABLE" ]; then
        echo "Database:     ONLINE ($DB_STATUS)"
    else
        echo "Database:     MAINTENANCE ($DB_STATUS)"
    fi

    # Check queue status
    QUEUE_STATUS=$(gcloud tasks queues describe "$QUEUE_NAME" \
        --location {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "unknown")
    if [ "$QUEUE_STATUS" = "RUNNING" ]; then
        echo "Task queue:   ONLINE ($QUEUE_STATUS)"
    else
        echo "Task queue:   MAINTENANCE ($QUEUE_STATUS)"
    fi

    echo ""

# =============================================================================
# Load Balancer & DNS
# =============================================================================
#
# Custom domain options for Cloud Run:
#
# Option A: Cloud Run Domain Mappings (simpler, no extra cost)
#   Supported in: asia-east1, asia-northeast1, asia-southeast1,
#   europe-north1, europe-west1, europe-west4, us-central1, us-east1,
#   us-east4, us-west1.
#   NOT supported in: australia-southeast1, australia-southeast2, and others.
#   Note: Domain mappings are still in preview and may have latency issues.
#   Set up via: gcloud beta run domain-mappings create ...
#
# Option B: Global Application Load Balancer (recommended for production)
#   Works in ALL regions. Required for regions without domain mapping support
#   (e.g. australia-southeast1). Provides a stable static IP, CDN
#   integration, and full control over SSL and routing.
#
# This section implements Option B. If your region supports domain mappings
# and you prefer the simpler setup, see the Cloud Run docs:
# https://cloud.google.com/run/docs/mapping-custom-domains
#
# Resources created (Option B):
#   - Global Static IP
#   - Network Endpoint Group (Serverless NEG) -> Points to Cloud Run
#   - Backend Service -> Routes traffic to NEG
#   - URL Map -> Directs all requests to Backend Service
#   - SSL Certificate -> Google-managed (auto-renewing)
#   - Target HTTPS Proxy -> Terminates SSL
#   - Forwarding Rule -> Listens on IP:443
#   - Target HTTP Proxy -> Optional for port 80
#   - Forwarding Rule -> Optional for IP:80
#
# Cost: Global external load balancers have a non-zero base cost even at low
# traffic. Check GCP pricing before you run this in production.
#
# =============================================================================

# Set up the Global Load Balancer
# Usage: just gcp lb-setup prod example.com
# Usage: just gcp lb-setup prod "example.com,www.example.com"
[arg('stage', pattern='dev|staging|prod')]
lb-setup stage domains: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "========================================================"
    echo "Setting up Global Load Balancer for: {{stage}}"
    DOMAINS_CSV="$(echo "{{domains}}" | tr -d '[:space:]')"
    if [ -z "$DOMAINS_CSV" ]; then
        echo "Error: domains must be a comma-separated list, e.g. 'example.com' or 'example.com,www.example.com'"
        exit 1
    fi
    echo "Domains: $DOMAINS_CSV"
    echo "========================================================"
    echo ""

    # Compute resource names
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="${APP_NAME}-web"
        SUFFIX=""
    else
        SERVICE="${APP_NAME}-web-{{stage}}"
        SUFFIX="-{{stage}}"
    fi

    IP_NAME="${APP_NAME}-ip${SUFFIX}"
    NEG_NAME="${APP_NAME}-neg${SUFFIX}"
    BACKEND_NAME="${APP_NAME}-backend${SUFFIX}"
    URL_MAP_NAME="${APP_NAME}-url-map${SUFFIX}"
    CERT_NAME="${APP_NAME}-cert${SUFFIX}"
    PROXY_NAME="${APP_NAME}-https-proxy${SUFFIX}"
    RULE_NAME="${APP_NAME}-lb-rule${SUFFIX}"
    HTTP_PROXY_NAME="${APP_NAME}-http-proxy${SUFFIX}"
    HTTP_RULE_NAME="${APP_NAME}-lb-rule-http${SUFFIX}"

    # 0. Verify the Cloud Run service exists
    echo "0. Checking Cloud Run service ($SERVICE)..."
    if gcloud run services describe "$SERVICE" \
        --region={{gcp_region}} --project {{gcp_project}} &>/dev/null; then
        echo "   Found"
    else
        echo "Error: Cloud Run service '$SERVICE' not found in region {{gcp_region}}."
        echo "Deploy it first: just gcp deploy {{stage}}"
        exit 1
    fi
    echo ""

    # 1. Reserve Static IP
    echo "1. Checking Static IP ($IP_NAME)..."
    if gcloud compute addresses describe "$IP_NAME" --global --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute addresses create "$IP_NAME" --global --project {{gcp_project}}
        echo "   Created"
    fi
    IP_ADDRESS=$(gcloud compute addresses describe "$IP_NAME" --global --project {{gcp_project}} --format="value(address)")
    echo "   -> IP: $IP_ADDRESS"
    echo ""

    # 2. Create NEG (Connects to Cloud Run)
    echo "2. Checking Network Endpoint Group ($NEG_NAME)..."
    if gcloud compute network-endpoint-groups describe "$NEG_NAME" \
        --region={{gcp_region}} --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute network-endpoint-groups create "$NEG_NAME" \
            --region={{gcp_region}} \
            --network-endpoint-type=serverless \
            --cloud-run-service="$SERVICE" \
            --project {{gcp_project}}
        echo "   Created (pointing to $SERVICE)"
    fi
    echo ""

    # 3. Create Backend Service
    echo "3. Checking Backend Service ($BACKEND_NAME)..."
    if ! gcloud compute backend-services describe "$BACKEND_NAME" --global --project {{gcp_project}} &>/dev/null; then
        gcloud compute backend-services create "$BACKEND_NAME" \
            --global \
            --protocol=HTTP \
            --project {{gcp_project}}
        echo "   Created service"
    else
        echo "   Service exists"
    fi

    CURRENT_TIMEOUT=$(gcloud compute backend-services describe "$BACKEND_NAME" --global --project {{gcp_project}} --format="value(timeoutSec)" 2>/dev/null || echo "")
    if [[ -n "$CURRENT_TIMEOUT" && "$CURRENT_TIMEOUT" != "30" ]]; then
        echo "   ! Backend Service timeoutSec is $CURRENT_TIMEOUT (unsupported for Serverless NEGs)."
        echo "     Resetting Backend Service timeout to default (30s) to satisfy GCP requirements."
        echo "     Note: Cloud Run request timeout is configured on the Cloud Run service (we deploy with {{gcp_cloud_run_request_timeout}})."
        gcloud compute backend-services update "$BACKEND_NAME" \
            --global \
            --timeout=30s \
            --project {{gcp_project}}
        echo "   Timeout configuration fixed"
    fi

    # Add NEG to Backend Service (idempotent check)
    if gcloud compute backend-services describe "$BACKEND_NAME" --global --project {{gcp_project}} \
        --format="value(backends[].group)" 2>/dev/null | grep -q "/$NEG_NAME$"; then
        echo "   Backend already attached"
    else
        gcloud compute backend-services add-backend "$BACKEND_NAME" \
            --global \
            --network-endpoint-group="$NEG_NAME" \
            --network-endpoint-group-region={{gcp_region}} \
            --project {{gcp_project}}
        echo "   Attached NEG to backend"
    fi
    echo ""

    # 4. Create URL Map
    echo "4. Checking URL Map ($URL_MAP_NAME)..."
    if gcloud compute url-maps describe "$URL_MAP_NAME" --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute url-maps create "$URL_MAP_NAME" \
            --default-service "$BACKEND_NAME" \
            --project {{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # 5. Create Managed SSL Certificate
    echo "5. Checking SSL Certificate ($CERT_NAME)..."
    if gcloud compute ssl-certificates describe "$CERT_NAME" --global --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute ssl-certificates create "$CERT_NAME" \
            --domains "$DOMAINS_CSV" \
            --global \
            --project {{gcp_project}}
        echo "   Created for: $DOMAINS_CSV"
    fi
    echo ""

    # 6. Create Target HTTPS Proxy
    echo "6. Checking Target Proxy ($PROXY_NAME)..."
    if gcloud compute target-https-proxies describe "$PROXY_NAME" --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute target-https-proxies create "$PROXY_NAME" \
            --ssl-certificates="$CERT_NAME" \
            --url-map="$URL_MAP_NAME" \
            --project {{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # 7. Create Forwarding Rule
    echo "7. Checking Forwarding Rule ($RULE_NAME)..."
    if gcloud compute forwarding-rules describe "$RULE_NAME" --global --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute forwarding-rules create "$RULE_NAME" \
            --address="$IP_NAME" \
            --target-https-proxy="$PROXY_NAME" \
            --global \
            --ports=443 \
            --project {{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # 8. Create Target HTTP Proxy (optional but recommended for http->https redirects)
    echo "8. Checking HTTP Target Proxy ($HTTP_PROXY_NAME)..."
    if gcloud compute target-http-proxies describe "$HTTP_PROXY_NAME" --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute target-http-proxies create "$HTTP_PROXY_NAME" \
            --url-map="$URL_MAP_NAME" \
            --project {{gcp_project}}
        echo "   Created"
    fi
    echo ""

    # 9. Create HTTP Forwarding Rule (port 80)
    echo "9. Checking HTTP Forwarding Rule ($HTTP_RULE_NAME)..."
    if gcloud compute forwarding-rules describe "$HTTP_RULE_NAME" --global --project {{gcp_project}} &>/dev/null; then
        echo "   Exists"
    else
        gcloud compute forwarding-rules create "$HTTP_RULE_NAME" \
            --address="$IP_NAME" \
            --target-http-proxy="$HTTP_PROXY_NAME" \
            --global \
            --ports=80 \
            --project {{gcp_project}}
        echo "   Created"
    fi
    echo ""

    echo "========================================================"
    echo "Load Balancer Setup Complete"
    echo "========================================================"
    echo ""
    echo "ACTION REQUIRED: Update your DNS records"
    echo ""
    echo "Create these records for: $DOMAINS_CSV"
    echo "--------------------------------------------------------"
    echo "Type:  A"
    echo "Value: $IP_ADDRESS"
    echo "TTL:   1 hour (or default)"
    echo "--------------------------------------------------------"
    echo ""
    echo "Note: Google-managed certificates can take 15-60 minutes to provision"
    echo "after DNS propagation. Until then, you may see SSL errors."

    echo ""
    echo "Optional hardening (recommended after you verify the LB works):"
    echo "  Restrict direct access to the *.run.app URL:"
    echo "    gcloud run services update \"$SERVICE\" \\"
    echo "      --ingress internal-and-cloud-load-balancing \\"
    echo "      --region {{gcp_region}} \\"
    echo "      --project {{gcp_project}}"

# =============================================================================
# Verification & Validation
# =============================================================================
#
# Commands to verify that infrastructure and deployments are working correctly.
#
# =============================================================================

# Validate entire GCP infrastructure (prod only)
validate-all: validate-gcp

# Validate GCP infrastructure is healthy
validate-gcp:
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    echo "Validating GCP Infrastructure"
    echo "======================================"
    echo ""

    TESTS_PASSED=0
    TESTS_FAILED=0

    GREEN='\033[0;32m'
    RED='\033[0;31m'
    NC='\033[0m'

    echo "1. Checking Cloud Run service..."
    if gcloud run services describe ${APP_NAME}-web \
        --region={{gcp_region}} \
        --project={{gcp_project}} &>/dev/null; then
        echo -e "${GREEN}OK${NC} Cloud Run service '${APP_NAME}-web' exists"
        ((TESTS_PASSED++))

        URL=$(gcloud run services describe ${APP_NAME}-web \
            --region={{gcp_region}} \
            --project={{gcp_project}} \
            --format="value(status.url)")
        echo "  URL: $URL"
    else
        echo -e "${RED}FAIL${NC} Cloud Run service not found"
        ((TESTS_FAILED++))
    fi
    echo ""

    echo "2. Checking service health..."
    URL=$(gcloud run services describe ${APP_NAME}-web \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)")

    if curl -s --max-time 10 "$URL" &>/dev/null; then
        echo -e "${GREEN}OK${NC} Service responds to requests"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}FAIL${NC} Service not responding"
        ((TESTS_FAILED++))
    fi
    echo ""

    echo "3. Checking for recent errors in logs..."
    ERROR_COUNT=$(gcloud logging read \
        "resource.type=cloud_run_revision AND resource.labels.service_name=${APP_NAME}-web AND severity>=ERROR" \
        --project={{gcp_project}} \
        --limit=10 \
        --format=json | jq '. | length')

    if [ "$ERROR_COUNT" -eq 0 ]; then
        echo -e "${GREEN}OK${NC} No recent errors in logs"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}FAIL${NC} Found $ERROR_COUNT recent errors"
        ((TESTS_FAILED++))
    fi
    echo ""

    echo "4. Checking Cloud SQL instance..."
    if gcloud sql instances describe ${APP_NAME}-db \
        --project={{gcp_project}} &>/dev/null; then
        echo -e "${GREEN}OK${NC} Cloud SQL instance exists"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}FAIL${NC} Cloud SQL instance not found"
        ((TESTS_FAILED++))
    fi
    echo ""

    echo "5. Checking GCS buckets..."
    BUCKET_COUNT=$(gcloud storage buckets list \
        --project={{gcp_project}} \
        --format=json | jq '. | length')

    if [ "$BUCKET_COUNT" -ge 2 ]; then
        echo -e "${GREEN}OK${NC} Found $BUCKET_COUNT storage buckets"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}FAIL${NC} Expected at least 2 buckets, found $BUCKET_COUNT"
        ((TESTS_FAILED++))
    fi
    echo ""

    echo "======================================"
    echo "GCP Validation Summary"
    echo "======================================"
    echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    echo ""

    if [ $TESTS_FAILED -eq 0 ]; then
        echo -e "${GREEN}All GCP tests passed!${NC}"
        exit 0
    else
        echo -e "${RED}Some GCP tests failed${NC}"
        exit 1
    fi

# Run tests against live GCP infrastructure
# Loads env from .envs/.test-on-gcp/.django and runs pytest
# Usage:
#   just gcp test-on-gcp                           # Run all GCP integration tests
#   just gcp test-on-gcp -k "connectivity"         # Run only connectivity tests
#   just gcp test-on-gcp --collect-only            # See which tests would run
test-on-gcp *args:
    #!/usr/bin/env bash
    set -euo pipefail

    ENV_FILE=".envs/.test-on-gcp/.django"

    if [ ! -f "$ENV_FILE" ]; then
        echo "Error: $ENV_FILE not found"
        echo ""
        echo "Create it by copying your GCP env file:"
        echo "  mkdir -p .envs/.test-on-gcp"
        echo "  cp .envs/.production/.google-cloud/.django $ENV_FILE"
        echo ""
        echo "Then set DJANGO_SETTINGS_MODULE=config.settings.test in the copy."
        exit 1
    fi

    echo "Loading environment from $ENV_FILE"
    set -a
    source "$ENV_FILE"
    set +a

    echo "Running tests against GCP..."
    echo ""
    uv run pytest tests/tests_integration/ {{args}} -v --log-cli-level=INFO

# Run E2E tests against deployed staging environment
# Tests the full flow: API -> Cloud Run Job -> Callback -> Worker
test-e2e *args:
    @if [ -z "${E2E_TEST_API_URL:-}" ]; then \
        echo "Error: E2E_TEST_API_URL not set"; \
        echo ""; \
        echo "Usage:"; \
        echo "  E2E_TEST_API_URL=https://your-staging-url.run.app/api/v1 \\"; \
        echo "  E2E_TEST_API_TOKEN=your-api-token \\"; \
        echo "  E2E_TEST_WORKFLOW_ID=workflow-uuid \\"; \
        echo "  just gcp test-e2e"; \
        exit 1; \
    fi
    @echo "Running E2E tests against: ${E2E_TEST_API_URL}"
    uv run --group dev pytest tests/tests_integration/test_e2e_workflow.py {{args}} -v --log-cli-level=INFO

# Verify a deployed environment with smoke tests
# Usage: just gcp verify-deployment dev | just gcp verify-deployment prod
[arg('stage', pattern='dev|staging|prod')]
verify-deployment stage *args: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "=============================================="
    echo "Post-Deployment Verification: {{stage}}"
    echo "=============================================="
    echo ""

    if ! command -v gcloud &> /dev/null; then
        echo "Error: gcloud CLI not found. Install Google Cloud SDK first."
        exit 1
    fi

    if ! gcloud auth print-identity-token &> /dev/null; then
        echo "Error: No valid gcloud credentials."
        echo "Run: gcloud auth login"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
    fi

    echo "Target services:"
    echo "  Web:    $WEB_SERVICE"
    echo "  Worker: $WORKER_SERVICE"
    echo ""

    echo "Checking service deployment..."
    WEB_URL=$(gcloud run services describe "$WEB_SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)" 2>/dev/null) || {
        echo "Error: Web service $WEB_SERVICE not found or not accessible"
        exit 1
    }
    WORKER_URL=$(gcloud run services describe "$WORKER_SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)" 2>/dev/null) || {
        echo "Error: Worker service $WORKER_SERVICE not found or not accessible"
        exit 1
    }

    echo "  Web URL:    $WEB_URL"
    echo "  Worker URL: $WORKER_URL"
    echo ""
    echo "Running smoke tests..."
    echo ""

    # Pass superuser credentials for authenticated tests (if available).
    # These are set in the .django env file. If not in the environment,
    # authenticated tests are skipped automatically.
    if [ -n "${SUPERUSER_USERNAME:-}" ] && [ -n "${SUPERUSER_PASSWORD:-}" ]; then
        echo "  Superuser credentials found - authenticated tests will run"
    else
        echo "  No superuser credentials - authenticated tests will be skipped"
        echo "  To enable: export SUPERUSER_USERNAME=... SUPERUSER_PASSWORD=..."
    fi
    echo ""

    SMOKE_TEST_STAGE={{stage}} uv run pytest tests/smoke/ {{args}} -v

    echo ""
    echo "=============================================="
    echo "PDV Complete: {{stage}}"
    echo "=============================================="

# Quick deployment verification (just check services are up)
[arg('stage', pattern='dev|staging|prod')]
verify-deployment-quick stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "Quick verification for {{stage}}..."

    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="${APP_NAME}-web"
        WORKER_SERVICE="${APP_NAME}-worker"
    else
        WEB_SERVICE="${APP_NAME}-web-{{stage}}"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
    fi

    WEB_URL=$(gcloud run services describe "$WEB_SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)" 2>/dev/null) || {
        echo "Web service not deployed"
        exit 1
    }

    WORKER_URL=$(gcloud run services describe "$WORKER_SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)" 2>/dev/null) || {
        echo "Worker service not deployed"
        exit 1
    }

    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$WEB_URL" --max-time 30) || HTTP_CODE="000"
    if [ "$HTTP_CODE" = "200" ]; then
        echo "Web service responding ($WEB_URL)"
    else
        echo "Web service returned $HTTP_CODE"
        exit 1
    fi

    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$WORKER_URL" --max-time 30) || HTTP_CODE="000"
    if [ "$HTTP_CODE" = "403" ]; then
        echo "Worker service IAM protected ($WORKER_URL)"
    else
        echo "Worker service returned $HTTP_CODE (expected 403)"
        exit 1
    fi

    echo ""
    echo "Quick verification passed for {{stage}}"

# =============================================================================
# Security Audit
# =============================================================================

# Check for common GCP security misconfigurations
# Run before releases or periodically to catch drift.
#
# Checks:
#   1. User-managed SA keys (should be zero)
#   2. SA role sprawl (lists roles per SA for review)
#   3. Worker ingress posture (should be internal)
#   4. Public bucket access (should only be on public/ prefix)
#
# Usage: just gcp security-audit prod
[arg('stage', pattern='dev|staging|prod')]
security-audit stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    PASS=0
    WARN=0

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "============================================="
    echo "Security audit for: {{stage}}"
    echo "============================================="
    echo ""

    # Determine stage-specific resource names
    if [ "{{stage}}" = "prod" ]; then
        MAIN_SA="${APP_NAME}-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        VALIDATOR_SA="${APP_NAME}-validator-prod@{{gcp_project}}.iam.gserviceaccount.com"
        WORKER_SERVICE="${APP_NAME}-worker"
        STORAGE_BUCKET="${APP_NAME}-storage"
    else
        MAIN_SA="${APP_NAME}-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        VALIDATOR_SA="${APP_NAME}-validator-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        WORKER_SERVICE="${APP_NAME}-worker-{{stage}}"
        STORAGE_BUCKET="${APP_NAME}-storage-{{stage}}"
    fi

    # 1. Check for user-managed SA keys (should be zero)
    echo "1. Checking for user-managed SA keys..."
    for sa in "$MAIN_SA" "$VALIDATOR_SA"; do
        sa_short="${sa%%@*}"
        KEYS=$(gcloud iam service-accounts keys list \
            --iam-account="$sa" \
            --managed-by=user \
            --project={{gcp_project}} \
            --format="value(name)" 2>/dev/null || echo "")
        if [ -z "$KEYS" ]; then
            echo "   PASS: $sa_short has no user-managed keys"
            ((PASS++))
        else
            KEY_COUNT=$(echo "$KEYS" | wc -l | tr -d ' ')
            echo "   WARN: $sa_short has $KEY_COUNT user-managed key(s) - consider deleting"
            ((WARN++))
        fi
    done
    echo ""

    # 2. Check SA role assignments
    echo "2. Checking SA role assignments..."
    for sa in "$MAIN_SA" "$VALIDATOR_SA"; do
        sa_short="${sa%%@*}"
        echo "   $sa_short:"
        ROLES=$(gcloud projects get-iam-policy {{gcp_project}} \
            --flatten="bindings[].members" \
            --filter="bindings.members:$sa" \
            --format="value(bindings.role)" 2>/dev/null || echo "(none)")
        if [ -z "$ROLES" ]; then
            echo "     (no project-level roles)"
        else
            echo "$ROLES" | while read -r role; do
                echo "     - $role"
            done
        fi
    done
    echo ""

    # 3. Check worker ingress posture
    echo "3. Checking worker ingress..."
    WORKER_INGRESS=$(gcloud run services describe "$WORKER_SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(spec.template.metadata.annotations.'run.googleapis.com/ingress')" 2>/dev/null || echo "unknown")
    if [ "$WORKER_INGRESS" = "internal" ]; then
        echo "   PASS: Worker ingress is 'internal'"
        ((PASS++))
    elif [ "$WORKER_INGRESS" = "unknown" ]; then
        echo "   SKIP: Worker service not found (not yet deployed?)"
    else
        echo "   WARN: Worker ingress is '$WORKER_INGRESS' (expected 'internal')"
        ((WARN++))
    fi
    echo ""

    # 4. Check bucket public access
    echo "4. Checking bucket public access..."
    # Find bindings where allUsers or allAuthenticatedUsers have access without conditions
    UNCONDITIONAL_PUBLIC=$(gcloud storage buckets get-iam-policy "gs://$STORAGE_BUCKET" \
        --project={{gcp_project}} \
        --format=json 2>/dev/null \
        | jq -r '
            .bindings[]?
            | select((.members // []) | any(. == "allUsers" or . == "allAuthenticatedUsers"))
            | select(.condition == null)
            | "UNCONDITIONAL: \(.role)"
        ' 2>/dev/null || echo "")
    if [ -z "$UNCONDITIONAL_PUBLIC" ]; then
        echo "   PASS: No unconditional public access on $STORAGE_BUCKET"
        ((PASS++))
    else
        echo "   WARN: Unconditional public access found on $STORAGE_BUCKET:"
        echo "$UNCONDITIONAL_PUBLIC" | while read -r line; do
            echo "     $line"
        done
        ((WARN++))
    fi
    echo ""

    # Summary
    echo "============================================="
    echo "Results: $PASS passed, $WARN warnings"
    echo "============================================="
    if [ "$WARN" -gt 0 ]; then
        exit 1
    fi

# =============================================================================
# Database Access
# =============================================================================

# Start a Cloud SQL Auth Proxy for local database access.
#
# The proxy creates an encrypted tunnel from your laptop to the Cloud SQL
# instance using your Google Cloud IAM identity. It listens on localhost:5432,
# so you can connect with any PostgreSQL client (psql, pgAdmin, DBeaver, etc.):
#
#   psql "postgresql://validibot_user:PASSWORD@localhost:5432/validibot"
#
# This is the recommended way to access the database after the public IP is
# removed. It works from any network (home, coffee shop, VPN) because
# authentication is identity-based (your gcloud credentials), not IP-based.
#
# Prerequisites:
#   - cloud-sql-proxy binary installed (brew install cloud-sql-proxy)
#   - Authenticated with gcloud (gcloud auth application-default login)
#
# Press Ctrl+C to stop the proxy.
#
# Usage: just gcp db-proxy dev | just gcp db-proxy prod
[arg('stage', pattern='dev|staging|prod')]
db-proxy stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [ "{{stage}}" = "prod" ]; then
        DB_INSTANCE="${APP_NAME}-db"
    else
        DB_INSTANCE="${APP_NAME}-db-{{stage}}"
    fi

    CONNECTION_NAME="{{gcp_project}}:{{gcp_region}}:${DB_INSTANCE}"

    # Check if cloud-sql-proxy is installed
    if ! command -v cloud-sql-proxy &>/dev/null; then
        echo "Error: cloud-sql-proxy is not installed."
        echo ""
        echo "Install it with:"
        echo "  brew install cloud-sql-proxy        # macOS"
        echo "  # or download from https://cloud.google.com/sql/docs/postgres/sql-proxy"
        echo ""
        echo "Then authenticate:"
        echo "  gcloud auth application-default login"
        exit 1
    fi

    echo "Starting Cloud SQL Auth Proxy for {{stage}}..."
    echo ""
    echo "  Instance:  $CONNECTION_NAME"
    echo "  Listening: localhost:5432"
    echo ""
    echo "Connect with:"
    echo "  psql \"postgresql://validibot_user:PASSWORD@localhost:5432/validibot\""
    echo ""
    echo "Press Ctrl+C to stop."
    echo ""

    cloud-sql-proxy "$CONNECTION_NAME" \
        --port 5432 \
        --auto-iam-authn

# =============================================================================
# Helpers
# =============================================================================

# Authenticate Docker with Google Artifact Registry (run once)
auth:
    gcloud auth configure-docker {{gcp_region}}-docker.pkg.dev

# Open the Cloud Run console in browser for a stage
# Usage: just gcp console dev | just gcp console prod
[arg('stage', pattern='dev|staging|prod')]
console stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="${APP_NAME}-web"; else SERVICE="${APP_NAME}-web-{{stage}}"; fi
    open "https://console.cloud.google.com/run/detail/{{gcp_region}}/$SERVICE/metrics?project={{gcp_project}}"

# Connect local Django shell to a Cloud SQL database via proxy
# Uses Cloud SQL Proxy for secure connection.
# Requires: DATABASE_PASSWORD environment variable set
# Usage: DATABASE_PASSWORD='yourpass' just gcp local-to-gcp-shell dev
[arg('stage', pattern='dev|staging|prod')]
local-to-gcp-shell stage: _require-gcp-config
    #!/usr/bin/env bash
    set -euo pipefail
    APP_NAME="{{app_name}}"

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ -z "${DATABASE_PASSWORD:-}" ]; then
        echo "Error: DATABASE_PASSWORD environment variable not set"
        echo ""
        echo "Usage:"
        echo "  DATABASE_PASSWORD='your-password' just gcp local-to-gcp-shell {{stage}}"
        echo ""
        echo "Get the password from Secret Manager or your .envs/.{{stage}}/.django file"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        DB_INSTANCE="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db"
    else
        DB_INSTANCE="{{gcp_project}}:{{gcp_region}}:${APP_NAME}-db-{{stage}}"
    fi

    echo "Starting Cloud SQL Proxy for {{stage}}..."
    cloud-sql-proxy "$DB_INSTANCE" &
    PROXY_PID=$!

    sleep 2

    export DATABASE_URL="postgres://validibot_user:${DATABASE_PASSWORD}@localhost:5432/validibot"
    export DJANGO_SETTINGS_MODULE="config.settings.local"

    echo ""
    echo "Connected to {{stage}} database via Cloud SQL Proxy"
    echo "Be careful - changes affect live data!"
    echo "Press Ctrl+D to exit shell, then Ctrl+C to stop proxy"
    echo ""

    uv run python manage.py shell || true

    echo "Stopping Cloud SQL Proxy..."
    kill $PROXY_PID 2>/dev/null || true

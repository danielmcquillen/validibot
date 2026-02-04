# =============================================================================
# Validibot - Google Cloud Platform Deployment
# =============================================================================
#
# Commands for deploying and managing Validibot on Google Cloud Platform.
#
# Architecture:
#   - Cloud Run: Web and Worker services (containerized Django)
#   - Cloud SQL: PostgreSQL database (managed)
#   - Cloud Storage: Media and file storage (GCS buckets)
#   - Cloud Tasks: Background job queue
#   - Cloud Scheduler: Cron-like scheduled tasks
#   - Secret Manager: Environment variables and secrets
#   - Cloud KMS: Cryptographic key management for credential signing
#   - Global Load Balancer: HTTPS termination and custom domains
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Docker configured for Artifact Registry: just gcp auth
#   - Project and region configured below
#
# Quick start (new environment):
#   just gcp init-stage dev           # Create infrastructure
#   just gcp secrets-init dev         # Create env file template
#   # Edit .envs/.dev/.django
#   just gcp secrets dev              # Upload secrets
#   just gcp deploy-all dev           # Deploy services
#   just gcp migrate dev              # Run migrations
#   just gcp setup-data dev           # Seed initial data
#
# Daily operations:
#   just gcp deploy prod              # Deploy web service to prod
#   just gcp logs prod                # View logs
#   just gcp status prod              # Check service status
#
# =============================================================================

# Use bash for shell commands (more predictable than sh)
set shell := ["bash", "-cu"]

# Ensure gcloud SDK is in PATH (needed for docker-credential-gcloud)
# This handles the case where gcloud is installed but not in PATH
export PATH := env_var("HOME") + "/google-cloud-sdk/bin:" + env_var("PATH")

# =============================================================================
# GCP Project Configuration
# =============================================================================
#
# These settings are specific to the Validibot GCP project.
# Update these if deploying to a different GCP project.
#
# Region choice (australia-southeast1):
#   - Closest to primary user base
#   - Has Cloud Run, Cloud SQL, Cloud Tasks, etc.
#   - Note: Domain mapping not available, so we use Global Load Balancer
#
# =============================================================================

gcp_project := "project-a509c806-3e21-4fbc-b19"
gcp_region := "australia-southeast1"

# Docker image path in Artifact Registry
# Format: {region}-docker.pkg.dev/{project}/{repository}/{image}
gcp_image := "australia-southeast1-docker.pkg.dev/" + gcp_project + "/validibot/validibot-web"

# Cloud Run request timeout (1 hour)
# Long timeout needed for validator jobs that process large files
gcp_cloud_run_request_timeout := "3600s"

# Cloud Scheduler timezone for cron jobs
gcp_scheduler_timezone := "Australia/Sydney"

# Validator container registry path
validator_repo := "australia-southeast1-docker.pkg.dev/" + gcp_project + "/validibot"

# Git SHA for image tagging (inherited from common.just via root justfile)
git_sha := `git rev-parse --short HEAD`

# =============================================================================
# Service Account Configuration
# =============================================================================
#
# Each stage has its own service account for security isolation:
#   - validibot-cloudrun-prod@{project}.iam.gserviceaccount.com
#   - validibot-cloudrun-dev@{project}.iam.gserviceaccount.com
#   - validibot-cloudrun-staging@{project}.iam.gserviceaccount.com
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
        echo "validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"; \
    else \
        echo "validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"; \
    fi

# =============================================================================
# Build & Push
# =============================================================================

# Build Docker image for Cloud Run (linux/amd64 platform)
# Tags with both git SHA (for traceability) and 'latest' (for convenience)
#
# Why linux/amd64?
#   Cloud Run runs on Linux x86_64, so we must build for that platform
#   even when building on Apple Silicon (arm64) Macs.
build:
    @echo "Building image: {{gcp_image}}:{{git_sha}}"
    docker build --platform linux/amd64 \
        -f compose/production/django/Dockerfile \
        -t {{gcp_image}}:{{git_sha}} \
        -t {{gcp_image}}:latest .

# Push Docker image to Google Artifact Registry
# Requires: gcloud auth configure-docker (run 'just gcp auth' first)
push:
    @echo "Pushing image: {{gcp_image}}:{{git_sha}}"
    docker push {{gcp_image}}:{{git_sha}}
    docker push {{gcp_image}}:latest

# =============================================================================
# Deploy Services
# =============================================================================

# Deploy web service to a specific stage
# This is the main user-facing service that handles HTTP requests.
#
# Usage: just gcp deploy dev | just gcp deploy prod
#
# Key configuration:
#   - min-instances: prod=1 (avoid cold starts), dev/staging=0 (cost saving)
#   - max-instances: prod=4, dev/staging=2
#   - memory: 1Gi (sufficient for Django + gunicorn)
#   - allow-unauthenticated: public web access
deploy stage: build push
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute environment-specific names
    # Prod keeps 1 instance warm to avoid cold starts; dev/staging scale to zero
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="validibot-web"
        SA="validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db"
        SECRET="django-env"
        MIN_INSTANCES=1
        MAX_INSTANCES=4
    else
        SERVICE="validibot-web-{{stage}}"
        SA="validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db-{{stage}}"
        SECRET="django-env-{{stage}}"
        MIN_INSTANCES=0
        MAX_INSTANCES=2
    fi

    echo "Deploying $SERVICE to Cloud Run ({{stage}})..."
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
        --allow-unauthenticated \
        --project {{gcp_project}}

    echo ""
    echo "Web service deployed to {{stage}} (min-instances=$MIN_INSTANCES)"

# Deploy worker service to a specific stage
# The worker handles background tasks triggered by Cloud Tasks.
#
# Key differences from web service:
#   - no-allow-unauthenticated: Only Cloud Tasks/Scheduler can invoke
#   - Always scales to zero (no warm instances needed)
#   - Same image as web, different APP_ROLE env var
#
# Usage: just gcp deploy-worker dev | just gcp deploy-worker prod
deploy-worker stage: build push
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="validibot-worker"
        SA="validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db"
        SECRET="django-env"
    else
        SERVICE="validibot-worker-{{stage}}"
        SA="validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db-{{stage}}"
        SECRET="django-env-{{stage}}"
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
        --min-instances 0 \
        --max-instances 2 \
        --memory 1Gi \
        --timeout {{gcp_cloud_run_request_timeout}} \
        --project {{gcp_project}}

    echo ""
    echo "Worker service deployed to {{stage}}"

# Deploy both web and worker to a stage
# Usage: just gcp deploy-all dev | just gcp deploy-all prod
deploy-all stage: (deploy stage) (deploy-worker stage)
    @echo ""
    @echo "All services deployed to {{stage}}"

# =============================================================================
# Secrets Management
# =============================================================================

# Upload secrets for a specific stage
# Reads from local .envs/ directory and uploads to GCP Secret Manager.
#
# Usage: just gcp secrets dev | just gcp secrets prod
#
# Files:
#   - .envs/.dev/.django -> django-env-dev
#   - .envs/.staging/.django -> django-env-staging
#   - .envs/.production/.django -> django-env
#
# After uploading, redeploy services to pick up new secrets.
secrets stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute secret name and source file
    if [ "{{stage}}" = "prod" ]; then
        SECRET_NAME="django-env"
        ENV_FILE=".envs/.production/.django"
    else
        SECRET_NAME="django-env-{{stage}}"
        ENV_FILE=".envs/.{{stage}}/.django"
    fi

    # Check if env file exists
    if [ ! -f "$ENV_FILE" ]; then
        echo "Error: $ENV_FILE not found"
        echo ""
        echo "Create the environment file first. For dev, copy from production:"
        echo "  mkdir -p .envs/.dev"
        echo "  cp .envs/.production/.django .envs/.dev/.django"
        echo "  # Then edit .envs/.dev/.django with dev-specific values"
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

# Create an environment file template for dev or staging
# Copies from production template and provides instructions for customization.
#
# Usage: just gcp secrets-init dev | just gcp secrets-init staging
secrets-init stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{stage}}" =~ ^(dev|staging)$ ]]; then
        echo "Error: stage must be 'dev' or 'staging'"
        exit 1
    fi
    TARGET_DIR=".envs/.{{stage}}"
    TARGET_FILE="$TARGET_DIR/.django"
    PROD_FILE=".envs/.production/.django"
    if [ ! -f "$PROD_FILE" ]; then
        echo "Error: Production file not found at $PROD_FILE"
        exit 1
    fi
    mkdir -p "$TARGET_DIR"
    if [ -s "$TARGET_FILE" ]; then
        echo "Error: $TARGET_FILE already exists and is not empty"
        echo "Edit it directly or remove it first."
        exit 1
    fi
    echo "Creating {{stage}} environment file from production template..."
    cp "$PROD_FILE" "$TARGET_FILE"
    echo ""
    echo "Created $TARGET_FILE (copy of production)"
    echo ""
    echo "IMPORTANT - Edit the following values for {{stage}}:"
    echo "  - DATABASE_URL: Update to use validibot-db-{{stage}}"
    echo "  - DJANGO_ALLOWED_HOSTS: Add the {{stage}} service URL"
    echo "  - SITE_URL: Set to the {{stage}} web service URL (the *.run.app URL is fine)"
    echo "  - WORKER_URL: Set to the {{stage}} worker service URL (the *.run.app URL)"
    echo "  - CLOUD_SQL_CONNECTION_NAME: Change to validibot-db-{{stage}}"
    echo ""
    echo "Next steps:"
    echo "  1. Edit $TARGET_FILE                   # Update stage-specific values"
    echo "  2. Run: just gcp secrets {{stage}}      # Upload secrets"
    echo "  3. Run: just gcp deploy {{stage}}       # Deploy"

# =============================================================================
# Operations
# =============================================================================

# View recent Cloud Run logs (last 50 entries)
# Usage: just gcp logs dev | just gcp logs prod
logs stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$SERVICE" \
        --project {{gcp_project}} \
        --limit 50 \
        --format="table(timestamp,severity,textPayload)"

# View logs and follow (stream new logs as they arrive)
# Usage: just gcp logs-follow dev | just gcp logs-follow prod
logs-follow stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    gcloud logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$SERVICE" \
        --project {{gcp_project}} \
        --format="table(timestamp,severity,textPayload)"

# Pause the service (block public access, but keep it deployed)
# Useful for maintenance or cost control without full teardown.
# Usage: just gcp pause dev | just gcp pause prod
pause stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    gcloud run services update "$SERVICE" \
        --region {{gcp_region}} \
        --ingress internal \
        --project {{gcp_project}}
    echo "$SERVICE paused. Public access blocked."

# Resume the service (restore public access)
# Usage: just gcp resume dev | just gcp resume prod
resume stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    gcloud run services update "$SERVICE" \
        --region {{gcp_region}} \
        --ingress all \
        --project {{gcp_project}}
    echo "$SERVICE resumed. Public access restored."

# Show current service status and URL
# Usage: just gcp status dev | just gcp status prod
status stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="validibot-web"
        WORKER_SERVICE="validibot-worker"
    else
        WEB_SERVICE="validibot-web-{{stage}}"
        WORKER_SERVICE="validibot-worker-{{stage}}"
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
open stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
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
health-check stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    URL=$(gcloud run services describe "$SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(status.url)")
    echo "Checking: $URL"
    curl -s -o /dev/null -w "HTTP Status: %{http_code}\nTime: %{time_total}s\n" "$URL"

# =============================================================================
# Management Commands
# =============================================================================

# Run database migrations for a stage
# Creates a temporary Cloud Run Job to run migrations against the database.
#
# Usage: just gcp migrate dev | just gcp migrate prod
migrate stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="validibot-migrate"
        SA="validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db"
        SECRET="django-env"
    else
        JOB_NAME="validibot-migrate-{{stage}}"
        SA="validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    echo "Running migrate on {{stage}}..."

    # Delete existing job if present
    gcloud run jobs delete "$JOB_NAME" --region {{gcp_region}} --project {{gcp_project}} --quiet 2>/dev/null || true

    # Create and run job
    gcloud run jobs create "$JOB_NAME" \
        --image {{gcp_image}}:latest \
        --region {{gcp_region}} \
        --service-account "$SA" \
        --set-cloudsql-instances "$DB" \
        --set-secrets=/secrets/.env="$SECRET":latest \
        --memory 1Gi \
        --command "/bin/bash" \
        --args "-c,set -a && source /secrets/.env && set +a && python manage.py migrate --noinput" \
        --project {{gcp_project}}

    # Execute and capture the execution name
    EXECUTION=$(gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(metadata.name)")

    echo "Execution: $EXECUTION"
    echo "Streaming logs (Ctrl+C to stop watching, job will continue)..."
    echo ""

    # Stream logs until job completes
    gcloud beta run jobs executions logs "$EXECUTION" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --follow

    echo ""
    echo "migrate completed on {{stage}}"

# Run setup_validibot to initialize site and default data
# Configures site domain, validators, roles, workspaces, etc.
# Usage: just gcp setup-data dev | just gcp setup-data prod
setup-data stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="validibot-setup"
        SA="validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db"
        SECRET="django-env"
    else
        JOB_NAME="validibot-setup-{{stage}}"
        SA="validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    echo "Running setup_validibot on {{stage}}..."

    # Delete existing job if present
    gcloud run jobs delete "$JOB_NAME" --region {{gcp_region}} --project {{gcp_project}} --quiet 2>/dev/null || true

    # Create and run job
    gcloud run jobs create "$JOB_NAME" \
        --image {{gcp_image}}:latest \
        --region {{gcp_region}} \
        --service-account "$SA" \
        --set-cloudsql-instances "$DB" \
        --set-secrets=/secrets/.env="$SECRET":latest \
        --memory 1Gi \
        --command "/bin/bash" \
        --args "-c,set -a && source /secrets/.env && set +a && python manage.py setup_validibot --noinput" \
        --project {{gcp_project}}

    # Execute and capture the execution name
    EXECUTION=$(gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(metadata.name)")

    echo "Execution: $EXECUTION"
    echo "Streaming logs (Ctrl+C to stop watching, job will continue)..."
    echo ""

    # Stream logs until job completes
    gcloud beta run jobs executions logs "$EXECUTION" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --follow

    echo ""
    echo "setup_validibot completed on {{stage}}"

# Run any Django management command on a deployed environment
# Creates a temporary Cloud Run Job, executes the command, then cleans up.
#
# Usage: just gcp management-cmd prod "seed_plans --force --skip-stripe"
# Usage: just gcp management-cmd dev "shell"
# Usage: just gcp management-cmd prod "migrate --check"
management-cmd stage command:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="validibot-web"
        SA="validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db"
        SECRET="django-env"
    else
        SERVICE="validibot-web-{{stage}}"
        SA="validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db-{{stage}}"
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
        --command "/bin/bash" \
        --args "-c,set -a && source /secrets/.env && set +a && python manage.py {{command}}" \
        --project {{gcp_project}}

    # Execute and capture the execution name
    EXECUTION=$(gcloud run jobs execute "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(metadata.name)")

    echo "Execution: $EXECUTION"
    echo "Streaming logs (Ctrl+C to stop watching, job will continue)..."
    echo ""

    # Stream logs until job completes
    gcloud beta run jobs executions logs "$EXECUTION" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --follow

    # Clean up job after completion
    echo ""
    echo "Cleaning up job..."
    gcloud run jobs delete "$JOB_NAME" --region {{gcp_region}} --project {{gcp_project}} --quiet

    echo "Command completed on {{stage}}"

# View logs from a Cloud Run job execution
# Usage: just gcp job-logs validibot-migrate-dev
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
#   1. just gcp init-stage dev          # Create infrastructure
#   2. just gcp secrets-init dev        # Create env file template
#   3. Edit .envs/.dev/.django          # Configure dev-specific values
#   4. just gcp secrets dev             # Upload secrets
#   5. just gcp deploy-all dev          # Deploy services
#   6. just gcp migrate dev             # Run migrations
#   7. just gcp setup-data dev          # Seed initial data
#
# =============================================================================

# Initialize infrastructure for a new stage (creates service account, database, etc.)
# Run this ONCE when setting up a new environment. Idempotent - safe to re-run.
#
# Usage: just gcp init-stage dev
init-stage stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage parameter
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "============================================="
    echo "Initializing GCP infrastructure for: {{stage}}"
    echo "============================================="
    echo ""

    # Prod uses names without suffix, dev/staging use stage suffix
    if [ "{{stage}}" = "prod" ]; then
        SA_NAME="validibot-cloudrun-prod"
        DB_INSTANCE="validibot-db"
        SECRET_NAME="django-env"
        QUEUE_NAME="validibot-tasks"
    else
        SA_NAME="validibot-cloudrun-{{stage}}"
        DB_INSTANCE="validibot-db-{{stage}}"
        SECRET_NAME="django-env-{{stage}}"
        QUEUE_NAME="validibot-validation-queue-{{stage}}"
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

    # Step 3: Create Cloud SQL instance (db-f1-micro for dev, small for staging)
    echo "3. Creating Cloud SQL instance: $DB_INSTANCE"
    if gcloud sql instances describe "$DB_INSTANCE" --project={{gcp_project}} &>/dev/null; then
        echo "   Database instance already exists"
    else
        TIER="db-f1-micro"
        if [ "{{stage}}" = "staging" ]; then
            TIER="db-g1-small"
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
            --project={{gcp_project}}
        echo "   Created"
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

    # Step 6: Create GCS buckets
    echo "6. Creating GCS buckets"
    if [ "{{stage}}" = "prod" ]; then
        MEDIA_BUCKET="validibot-media"
        FILES_BUCKET="validibot-files"
    else
        MEDIA_BUCKET="validibot-media-{{stage}}"
        FILES_BUCKET="validibot-files-{{stage}}"
    fi

    for BUCKET in "$MEDIA_BUCKET" "$FILES_BUCKET"; do
        if gcloud storage buckets describe "gs://$BUCKET" --project={{gcp_project}} &>/dev/null; then
            echo "   Bucket '$BUCKET' already exists"
        else
            gcloud storage buckets create "gs://$BUCKET" \
                --location={{gcp_region}} \
                --project={{gcp_project}}
            echo "   Bucket '$BUCKET' created"
        fi

        # Grant objectUser role on this specific bucket
        gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
            --member="serviceAccount:$SA_EMAIL" \
            --role="roles/storage.objectUser" \
            --project={{gcp_project}} \
            --quiet >/dev/null
        echo "   Granted objectUser on $BUCKET"
    done
    echo ""

    # Step 7: Grant KMS permissions for credential signing
    echo "7. Granting KMS permissions"
    KMS_KEYRING="validibot-keys"
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

    # Summary
    echo "============================================="
    echo "Infrastructure setup complete for {{stage}}"
    echo "============================================="
    echo ""
    echo "Resources created:"
    echo "  Service account: $SA_EMAIL"
    echo "  Cloud SQL: $DB_INSTANCE (database: $DB_NAME)"
    echo "  Cloud Tasks queue: $QUEUE_NAME"
    echo "  GCS buckets: $MEDIA_BUCKET, $FILES_BUCKET"
    echo "  KMS permissions: cloudkms.viewer, cloudkms.signerVerifier"
    echo "  Secret: $SECRET_NAME"
    echo ""
    if [ "{{stage}}" = "prod" ]; then
        ENV_PATH=".envs/.production/.django"
    else
        ENV_PATH=".envs/.{{stage}}/.django"
    fi

    echo "Next steps:"
    echo "  1. Edit env file:       Edit $ENV_PATH with:"
    echo "                          - POSTGRES_PASSWORD and DATABASE_URL (use password shown above)"
    echo "                          - DJANGO_ALLOWED_HOSTS (add Cloud Run URL after first deploy)"
    echo "                          - DJANGO_SECRET_KEY (generate a new one)"
    echo "  2. Upload secrets:      just gcp secrets {{stage}}"
    echo "  3. Deploy services:     just gcp deploy-all {{stage}}"
    echo "  4. Run migrations:      just gcp migrate {{stage}}"
    echo "  5. Seed data:           just gcp setup-data {{stage}}"
    echo "  6. Set up scheduler:    just gcp scheduler-setup {{stage}}"
    echo "  7. Deploy validators:   just gcp validators-deploy-all {{stage}}"
    echo ""

# List all resources for a stage (useful for verification)
list-resources stage:
    #!/usr/bin/env bash
    set -euo pipefail

    echo "Resources for stage: {{stage}}"
    echo "================================"
    echo ""

    if [ "{{stage}}" = "prod" ]; then
        SERVICE="validibot-web"
        WORKER="validibot-worker"
        DB="validibot-db"
        SECRET="django-env"
        SA="validibot-cloudrun-prod"
    else
        SERVICE="validibot-web-{{stage}}"
        WORKER="validibot-worker-{{stage}}"
        DB="validibot-db-{{stage}}"
        SECRET="django-env-{{stage}}"
        SA="validibot-cloudrun-{{stage}}"
    fi

    echo "Cloud Run Services:"
    gcloud run services describe "$SERVICE" --region={{gcp_region}} --project={{gcp_project}} --format="value(status.url)" 2>/dev/null && echo "  $SERVICE" || echo "  $SERVICE (not deployed)"
    gcloud run services describe "$WORKER" --region={{gcp_region}} --project={{gcp_project}} --format="value(status.url)" 2>/dev/null && echo "  $WORKER" || echo "  $WORKER (not deployed)"
    echo ""

    echo "Cloud SQL:"
    gcloud sql instances describe "$DB" --project={{gcp_project}} --format="value(state)" 2>/dev/null && echo "  $DB" || echo "  $DB (not found)"
    echo ""

    echo "Secrets:"
    gcloud secrets describe "$SECRET" --project={{gcp_project}} &>/dev/null && echo "  $SECRET" || echo "  $SECRET (not found)"
    echo ""

    echo "Service Account:"
    gcloud iam service-accounts describe "$SA@{{gcp_project}}.iam.gserviceaccount.com" --project={{gcp_project}} &>/dev/null && echo "  $SA" || echo "  $SA (not found)"

# =============================================================================
# Cloud Scheduler
# =============================================================================
#
# Cloud Scheduler replaces Celery Beat for periodic tasks. Each job calls an
# HTTP endpoint on the worker service using OIDC authentication.
#
# Prerequisites:
#   - Worker service deployed (validibot-worker)
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
scheduler-setup stage:
    #!/usr/bin/env bash
    set -euo pipefail

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute stage-specific values
    if [ "{{stage}}" = "prod" ]; then
        WORKER_SERVICE="validibot-worker"
        SCHEDULER_SA="validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        JOB_SUFFIX=""
    else
        WORKER_SERVICE="validibot-worker-{{stage}}"
        SCHEDULER_SA="validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
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
                --description "$description"
        fi
        echo "   Done"
        echo ""
    }

    # Job 1: Clear expired sessions (daily at 2 AM)
    create_or_update_job \
        "validibot-clear-sessions${JOB_SUFFIX}" \
        "0 2 * * *" \
        "/api/v1/scheduled/clear-sessions/" \
        "Clear expired Django sessions ({{stage}})"

    # Job 2: Cleanup idempotency keys (daily at 3 AM)
    create_or_update_job \
        "validibot-cleanup-idempotency-keys${JOB_SUFFIX}" \
        "0 3 * * *" \
        "/api/v1/scheduled/cleanup-idempotency-keys/" \
        "Delete expired API idempotency keys - 24h TTL ({{stage}})"

    # Job 3: Cleanup callback receipts (weekly Sunday at 4 AM)
    create_or_update_job \
        "validibot-cleanup-callback-receipts${JOB_SUFFIX}" \
        "0 4 * * 0" \
        "/api/v1/scheduled/cleanup-callback-receipts/" \
        "Delete old validator callback receipts - 30 day retention ({{stage}})"

    # Job 4: Purge expired submissions (hourly at :00)
    create_or_update_job \
        "validibot-purge-expired-submissions${JOB_SUFFIX}" \
        "0 * * * *" \
        "/api/v1/scheduled/purge-expired-submissions/" \
        "Purge submission content past retention period ({{stage}})"

    # Job 5: Process purge retries (every 5 minutes)
    create_or_update_job \
        "validibot-process-purge-retries${JOB_SUFFIX}" \
        "*/5 * * * *" \
        "/api/v1/scheduled/process-purge-retries/" \
        "Retry failed submission purges ({{stage}})"

    # Job 6: Cleanup stuck runs (every 10 minutes)
    create_or_update_job \
        "validibot-cleanup-stuck-runs${JOB_SUFFIX}" \
        "*/10 * * * *" \
        "/api/v1/scheduled/cleanup-stuck-runs/" \
        "Mark stuck validation runs as FAILED - 30min timeout ({{stage}})"

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
scheduler-delete-all stage:
    #!/usr/bin/env bash
    set -euo pipefail

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
        for job_base in validibot-clear-sessions validibot-cleanup-idempotency-keys validibot-cleanup-callback-receipts validibot-purge-expired-submissions validibot-process-purge-retries validibot-cleanup-stuck-runs; do
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
scheduler-pause-all stage:
    #!/usr/bin/env bash
    set -euo pipefail

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
    for job_base in validibot-clear-sessions validibot-cleanup-idempotency-keys validibot-cleanup-callback-receipts validibot-purge-expired-submissions validibot-process-purge-retries validibot-cleanup-stuck-runs; do
        job="${job_base}${JOB_SUFFIX}"
        echo "Pausing $job..."
        gcloud scheduler jobs pause "$job" \
            --project {{gcp_project}} \
            --location {{gcp_region}} \
            --quiet 2>/dev/null || echo "  (job not found or already paused)"
    done
    echo "All scheduler jobs paused for {{stage}}"

# Resume all scheduler jobs for a stage
scheduler-resume-all stage:
    #!/usr/bin/env bash
    set -euo pipefail

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
    for job_base in validibot-clear-sessions validibot-cleanup-idempotency-keys validibot-cleanup-callback-receipts validibot-purge-expired-submissions validibot-process-purge-retries validibot-cleanup-stuck-runs; do
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
#   - fmi: Functional Mockup Interface validator
#
# Build context is vb_validators_dev/ to include shared core utilities.
#
# =============================================================================

# Build a specific validator container (energyplus, fmi, etc.)
# Usage: just gcp validator-build energyplus
validator-build name:
    docker build --platform linux/amd64 \
        -f vb_validators_dev/validators/{{name}}/Dockerfile \
        -t {{validator_repo}}/validibot-validator-{{name}}:{{git_sha}} \
        -t {{validator_repo}}/validibot-validator-{{name}}:latest \
        vb_validators_dev

# Push a validator container
validator-push name:
    docker push {{validator_repo}}/validibot-validator-{{name}}:{{git_sha}}
    docker push {{validator_repo}}/validibot-validator-{{name}}:latest

# Build and push in one step
validator-build-push name: (validator-build name) (validator-push name)

# Deploy a Cloud Run Job for a validator to a specific stage
# Usage: just gcp validator-deploy energyplus dev | just gcp validator-deploy fmi prod
validator-deploy name stage: (validator-build-push name)
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    # Compute stage-specific names
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="validibot-validator-{{name}}"
        SA="validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
    else
        JOB_NAME="validibot-validator-{{name}}-{{stage}}"
        SA="validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
    fi
    echo "Deploying $JOB_NAME..."
    gcloud run jobs deploy "$JOB_NAME" \
        --image {{validator_repo}}/validibot-validator-{{name}}:{{git_sha}} \
        --region {{gcp_region}} \
        --service-account "$SA" \
        --max-retries 0 \
        --task-timeout 3600 \
        --set-env-vars VALIDATOR_VERSION={{git_sha}},VALIDIBOT_STAGE={{stage}} \
        --labels validator={{name}},version={{git_sha}},stage={{stage}} \
        --project {{gcp_project}}
    echo "$JOB_NAME deployed"

    # Grant the service account permission to run this job with overrides
    echo "Granting job runner permission to $SA on $JOB_NAME..."
    gcloud run jobs add-iam-policy-binding "$JOB_NAME" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --member="serviceAccount:$SA" \
        --role="projects/{{gcp_project}}/roles/validibot_job_runner"
    echo "IAM binding added"

# Build and deploy all validator jobs to a stage
# Usage: just gcp validators-deploy-all dev | just gcp validators-deploy-all prod
validators-deploy-all stage:
    just gcp validator-deploy energyplus {{stage}}
    just gcp validator-deploy fmi {{stage}}

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
            sync_to_bucket "validibot-files-dev" "dev"
            ;;
        staging)
            sync_to_bucket "validibot-files-staging" "staging"
            ;;
        prod)
            echo "WARNING: You are about to sync weather files to PRODUCTION"
            read -p "Are you sure? (yes/no): " confirm
            if [ "$confirm" != "yes" ]; then
                echo "Aborted."
                exit 1
            fi
            sync_to_bucket "validibot-files" "prod"
            ;;
        all)
            sync_to_bucket "validibot-files-dev" "dev"
            sync_to_bucket "validibot-files-staging" "staging"
            echo ""
            echo "WARNING: About to sync to PRODUCTION"
            read -p "Continue with prod sync? (yes/no): " confirm
            if [ "$confirm" == "yes" ]; then
                sync_to_bucket "validibot-files" "prod"
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

    ASSETS_PREFIX="validator_assets/weather_data"

    case "{{env}}" in
        dev)
            BUCKET="validibot-files-dev"
            ;;
        staging)
            BUCKET="validibot-files-staging"
            ;;
        prod)
            BUCKET="validibot-files"
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
maintenance-on stage:
    #!/usr/bin/env bash
    set -euo pipefail

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

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
        WEB_SERVICE="validibot-web"
        WORKER_SERVICE="validibot-worker"
        DB_INSTANCE="validibot-db"
        QUEUE_NAME="validibot-tasks"
        JOB_SUFFIX=""
    else
        WEB_SERVICE="validibot-web-{{stage}}"
        WORKER_SERVICE="validibot-worker-{{stage}}"
        DB_INSTANCE="validibot-db-{{stage}}"
        QUEUE_NAME="validibot-validation-queue-{{stage}}"
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
    for job_base in validibot-clear-sessions validibot-cleanup-idempotency-keys validibot-cleanup-callback-receipts validibot-purge-expired-submissions validibot-process-purge-retries validibot-cleanup-stuck-runs; do
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
maintenance-off stage:
    #!/usr/bin/env bash
    set -euo pipefail

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute stage-specific names
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="validibot-web"
        WORKER_SERVICE="validibot-worker"
        DB_INSTANCE="validibot-db"
        QUEUE_NAME="validibot-tasks"
        JOB_SUFFIX=""
    else
        WEB_SERVICE="validibot-web-{{stage}}"
        WORKER_SERVICE="validibot-worker-{{stage}}"
        DB_INSTANCE="validibot-db-{{stage}}"
        QUEUE_NAME="validibot-validation-queue-{{stage}}"
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
    for job_base in validibot-clear-sessions validibot-cleanup-idempotency-keys validibot-cleanup-callback-receipts validibot-purge-expired-submissions validibot-process-purge-retries validibot-cleanup-stuck-runs; do
        job="${job_base}${JOB_SUFFIX}"
        gcloud scheduler jobs resume "$job" \
            --project {{gcp_project}} \
            --location {{gcp_region}} \
            --quiet 2>/dev/null && echo "   Resumed $job" || echo "   - $job (not found)"
    done

    # 4. Restore public traffic to web service
    echo "4. Restoring public traffic to $WEB_SERVICE..."
    gcloud run services update "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --ingress all \
        --project {{gcp_project}} \
        --quiet
    echo "   Web service accepting public traffic"

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
maintenance-status stage:
    #!/usr/bin/env bash
    set -euo pipefail

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute stage-specific names
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="validibot-web"
        DB_INSTANCE="validibot-db"
        QUEUE_NAME="validibot-tasks"
    else
        WEB_SERVICE="validibot-web-{{stage}}"
        DB_INSTANCE="validibot-db-{{stage}}"
        QUEUE_NAME="validibot-validation-queue-{{stage}}"
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
# Creates a Global Application Load Balancer to provide a stable static IP
# and a global entrypoint (required because Cloud Run 'Domain Mapping' is not
# available in australia-southeast1).
#
# Resources created:
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
# Usage: just gcp lb-setup prod validibot.com
# Usage: just gcp lb-setup prod "validibot.com,www.validibot.com"
lb-setup stage domains:
    #!/usr/bin/env bash
    set -euo pipefail

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "========================================================"
    echo "Setting up Global Load Balancer for: {{stage}}"
    DOMAINS_CSV="$(echo "{{domains}}" | tr -d '[:space:]')"
    if [ -z "$DOMAINS_CSV" ]; then
        echo "Error: domains must be a comma-separated list, e.g. 'validibot.com' or 'validibot.com,www.validibot.com'"
        exit 1
    fi
    echo "Domains: $DOMAINS_CSV"
    echo "========================================================"
    echo ""

    # Compute resource names
    if [ "{{stage}}" = "prod" ]; then
        SERVICE="validibot-web"
        SUFFIX=""
    else
        SERVICE="validibot-web-{{stage}}"
        SUFFIX="-{{stage}}"
    fi

    IP_NAME="validibot-ip${SUFFIX}"
    NEG_NAME="validibot-neg${SUFFIX}"
    BACKEND_NAME="validibot-backend${SUFFIX}"
    URL_MAP_NAME="validibot-url-map${SUFFIX}"
    CERT_NAME="validibot-cert${SUFFIX}"
    PROXY_NAME="validibot-https-proxy${SUFFIX}"
    RULE_NAME="validibot-lb-rule${SUFFIX}"
    HTTP_PROXY_NAME="validibot-http-proxy${SUFFIX}"
    HTTP_RULE_NAME="validibot-lb-rule-http${SUFFIX}"

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

    echo "Validating GCP Infrastructure"
    echo "======================================"
    echo ""

    TESTS_PASSED=0
    TESTS_FAILED=0

    GREEN='\033[0;32m'
    RED='\033[0;31m'
    NC='\033[0m'

    echo "1. Checking Cloud Run service..."
    if gcloud run services describe validibot-web \
        --region={{gcp_region}} \
        --project={{gcp_project}} &>/dev/null; then
        echo -e "${GREEN}OK${NC} Cloud Run service 'validibot-web' exists"
        ((TESTS_PASSED++))

        URL=$(gcloud run services describe validibot-web \
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
    URL=$(gcloud run services describe validibot-web \
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
        "resource.type=cloud_run_revision AND resource.labels.service_name=validibot-web AND severity>=ERROR" \
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
    if gcloud sql instances describe validibot-db \
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
        echo "Create it first - see .envs/.test-on-gcp/.django.example"
        exit 1
    fi

    echo "Loading environment from $ENV_FILE"

    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        export "$key=$value"
    done < "$ENV_FILE"

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
    uv run --extra dev pytest tests/tests_integration/test_e2e_workflow.py {{args}} -v --log-cli-level=INFO

# Verify a deployed environment with smoke tests
# Usage: just gcp verify-deployment dev | just gcp verify-deployment prod
verify-deployment stage *args:
    #!/usr/bin/env bash
    set -euo pipefail

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
        WEB_SERVICE="validibot-web"
        WORKER_SERVICE="validibot-worker"
    else
        WEB_SERVICE="validibot-web-{{stage}}"
        WORKER_SERVICE="validibot-worker-{{stage}}"
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

    SMOKE_TEST_STAGE={{stage}} uv run pytest tests/smoke/ {{args}} -v

    echo ""
    echo "=============================================="
    echo "PDV Complete: {{stage}}"
    echo "=============================================="

# Quick deployment verification (just check services are up)
verify-deployment-quick stage:
    #!/usr/bin/env bash
    set -euo pipefail

    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "Quick verification for {{stage}}..."

    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="validibot-web"
        WORKER_SERVICE="validibot-worker"
    else
        WEB_SERVICE="validibot-web-{{stage}}"
        WORKER_SERVICE="validibot-worker-{{stage}}"
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
# Helpers
# =============================================================================

# Authenticate Docker with Google Artifact Registry (run once)
auth:
    gcloud auth configure-docker australia-southeast1-docker.pkg.dev

# Open the Cloud Run console in browser for a stage
# Usage: just gcp console dev | just gcp console prod
console stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    open "https://console.cloud.google.com/run/detail/{{gcp_region}}/$SERVICE/metrics?project={{gcp_project}}"

# Connect local Django shell to a Cloud SQL database via proxy
# Uses Cloud SQL Proxy for secure connection.
# Requires: DATABASE_PASSWORD environment variable set
# Usage: DATABASE_PASSWORD='yourpass' just gcp local-to-gcp-shell dev
local-to-gcp-shell stage:
    #!/usr/bin/env bash
    set -euo pipefail

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
        DB_INSTANCE="{{gcp_project}}:{{gcp_region}}:validibot-db"
    else
        DB_INSTANCE="{{gcp_project}}:{{gcp_region}}:validibot-db-{{stage}}"
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

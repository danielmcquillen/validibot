# =============================================================================
# Validibot Justfile
# =============================================================================
#
# Just is a modern command runner (like Make, but better).
# Install: brew install just
# Docs: https://just.systems/man/en/
#
# Usage:
#   just              # List all available commands
#   just <command>    # Run a command
#   just gcp-manage "shell"  # Pass arguments to commands
#
# Tips:
#   - Tab completion: Add to ~/.zshrc: eval "$(just --completions zsh)"
#   - Run from subdirectory: just will find the justfile automatically
#   - See what a command does: just --show <command>
#   - Dry run: just --dry-run <command>
#
# =============================================================================

# Load .env file if present (optional, for local dev)
set dotenv-load := false

# Use bash for shell commands (more predictable than sh)
set shell := ["bash", "-cu"]

# Ensure gcloud SDK is in PATH (needed for docker-credential-gcloud)
export PATH := env_var("HOME") + "/google-cloud-sdk/bin:" + env_var("PATH")

# =============================================================================
# Configuration Variables
# =============================================================================

# GCP Project Settings (shared across all stages)
gcp_project := "project-a509c806-3e21-4fbc-b19"
gcp_region := "australia-southeast1"
gcp_image := "australia-southeast1-docker.pkg.dev/" + gcp_project + "/validibot/validibot-web"

# Get git commit hash for image tagging
git_sha := `git rev-parse --short HEAD`

# =============================================================================
# Multi-Environment Configuration
# =============================================================================
#
# This project supports multiple deployment stages: dev, staging, prod
#
# Usage:
#   just gcp-deploy dev       # Deploy to development
#   just gcp-deploy prod      # Deploy to production
#   just gcp-status dev       # Check dev status
#   just gcp-logs prod        # View prod logs
#
# Resource naming convention:
#   dev:     validibot-web-dev, validibot-worker-dev, validibot-db-dev
#   staging: validibot-web-staging, validibot-worker-staging, validibot-db-staging
#   prod:    validibot-web, validibot-worker, validibot-db
#
# Each stage has:
#   - Separate Cloud Run services (web + worker)
#   - Separate Cloud Run validator jobs
#   - Separate Cloud SQL instance
#   - Separate secrets in Secret Manager
#   - Separate GCS buckets (validibot-media-dev, validibot-files-dev, etc.)
#
# =============================================================================

# Helper to compute service suffix (empty for prod, -dev/-staging otherwise)
# Usage in recipes: {{_suffix(stage)}}
[private]
_suffix stage:
    @if [ "{{stage}}" = "prod" ]; then echo ""; else echo "-{{stage}}"; fi

# Helper to compute full web service name
[private]
_web_service stage:
    @if [ "{{stage}}" = "prod" ]; then echo "validibot-web"; else echo "validibot-web-{{stage}}"; fi

# Helper to compute full worker service name
[private]
_worker_service stage:
    @if [ "{{stage}}" = "prod" ]; then echo "validibot-worker"; else echo "validibot-worker-{{stage}}"; fi

# Helper to compute database instance name
[private]
_db_instance stage:
    @if [ "{{stage}}" = "prod" ]; then echo "validibot-db"; else echo "validibot-db-{{stage}}"; fi

# Helper to compute secret name
[private]
_secret_name stage:
    @if [ "{{stage}}" = "prod" ]; then echo "django-env"; else echo "django-env-{{stage}}"; fi

# Helper to compute service account
[private]
_service_account stage:
    @if [ "{{stage}}" = "prod" ]; then \
        echo "validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"; \
    else \
        echo "validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"; \
    fi

# Production-only variables (used by validate-* and health-check commands)
gcp_service := "validibot-web"
gcp_sa := "validibot-cloudrun-prod@" + gcp_project + ".iam.gserviceaccount.com"
gcp_sql := gcp_project + ":" + gcp_region + ":validibot-db"

# =============================================================================
# Default Command - Show Help
# =============================================================================

# List all available commands (this is the default when you just run 'just')
default:
    @just --list

# =============================================================================
# Local Docker Development
# =============================================================================

# Start all local Docker containers in detached mode
up:
    docker compose -f docker-compose.local.yml up -d

# Stop all local Docker containers
down:
    docker compose -f docker-compose.local.yml down

# Rebuild and start all containers (use after changing Dockerfile or dependencies)
build:
    docker compose -f docker-compose.local.yml up -d --build

# Follow logs from all containers (Ctrl+C to exit)
logs:
    docker compose -f docker-compose.local.yml logs -f

# Show status of all containers
ps:
    docker compose -f docker-compose.local.yml ps

# Restart all containers (stop then start)
restart: down up

# Stop containers and remove volumes - WARNING: loses database data!
clean:
    docker compose -f docker-compose.local.yml down -v

# Open a bash shell in the Django container
shell:
    docker compose -f docker-compose.local.yml exec django bash

# Run Django migrations in the local container
migrate:
    docker compose -f docker-compose.local.yml exec django python manage.py migrate

# =============================================================================
# GCP Cloud Run - Build & Deploy
# =============================================================================

# Build Docker image for Cloud Run (linux/amd64 platform)
# Tags with both git SHA and 'latest'
gcp-build:
    @echo "Building image: {{gcp_image}}:{{git_sha}}"
    docker build --platform linux/amd64 \
        -f compose/production/django/Dockerfile \
        -t {{gcp_image}}:{{git_sha}} \
        -t {{gcp_image}}:latest .

# Push Docker image to Google Artifact Registry
gcp-push:
    @echo "Pushing image: {{gcp_image}}:{{git_sha}}"
    docker push {{gcp_image}}:{{git_sha}}
    docker push {{gcp_image}}:latest

# Deploy web service to a specific stage
# Usage: just gcp-deploy dev | just gcp-deploy prod
gcp-deploy stage: gcp-build gcp-push
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
        --allow-unauthenticated \
        --project {{gcp_project}}

    echo ""
    echo "✓ Web service deployed to {{stage}} (min-instances=$MIN_INSTANCES)"

# Deploy worker service to a specific stage
# Usage: just gcp-deploy-worker dev | just gcp-deploy-worker prod
gcp-deploy-worker stage: gcp-build gcp-push
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
        --project {{gcp_project}}

    echo ""
    echo "✓ Worker service deployed to {{stage}}"

# Deploy both web and worker to a stage
# Usage: just gcp-deploy-all dev | just gcp-deploy-all prod
gcp-deploy-all stage: (gcp-deploy stage) (gcp-deploy-worker stage)
    @echo ""
    @echo "✓ All services deployed to {{stage}}"

# =============================================================================
# GCP Cloud Run - Secrets Management
# =============================================================================

# Upload secrets for a specific stage
# Usage: just gcp-secrets dev | just gcp-secrets prod
# Files: .envs/.dev/.django, .envs/.production/.django
gcp-secrets stage:
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
    echo "✓ Secret $SECRET_NAME updated."
    echo "  Run 'just gcp-deploy {{stage}}' to apply changes."

# Create an environment file template for dev or staging
# Usage: just gcp-secrets-init dev | just gcp-secrets-init staging
gcp-secrets-init stage:
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
    echo "  - CLOUD_SQL_CONNECTION_NAME: Change to validibot-db-{{stage}}"
    echo "  - GCP_KMS_SIGNING_KEY: Change 'credential-signing' to 'credential-signing-{{stage}}'"
    echo "  - GCP_KMS_JWKS_KEYS: Change 'credential-signing' to 'credential-signing-{{stage}}'"
    echo ""
    echo "Next steps:"
    echo "  1. Run: just gcp-kms-setup {{stage}}    # Create the signing key"
    echo "  2. Edit $TARGET_FILE                   # Update stage-specific values"
    echo "  3. Run: just gcp-secrets {{stage}}      # Upload secrets"
    echo "  4. Run: just gcp-deploy {{stage}}       # Deploy"

# =============================================================================
# GCP Cloud Run - Operations
# =============================================================================

# View recent Cloud Run logs (last 50 entries)
# Usage: just gcp-logs dev | just gcp-logs prod
gcp-logs stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$SERVICE" \
        --project {{gcp_project}} \
        --limit 50 \
        --format="table(timestamp,severity,textPayload)"

# View logs and follow (stream new logs as they arrive)
# Usage: just gcp-logs-follow dev | just gcp-logs-follow prod
gcp-logs-follow stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    gcloud logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=$SERVICE" \
        --project {{gcp_project}} \
        --format="table(timestamp,severity,textPayload)"

# Pause the service (block public access, but keep it deployed)
# Usage: just gcp-pause dev | just gcp-pause prod
gcp-pause stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    gcloud run services update "$SERVICE" \
        --region {{gcp_region}} \
        --ingress internal \
        --project {{gcp_project}}
    echo "✓ $SERVICE paused. Public access blocked."

# Resume the service (restore public access)
# Usage: just gcp-resume dev | just gcp-resume prod
gcp-resume stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    gcloud run services update "$SERVICE" \
        --region {{gcp_region}} \
        --ingress all \
        --project {{gcp_project}}
    echo "✓ $SERVICE resumed. Public access restored."

# Show current service status and URL
# Usage: just gcp-status dev | just gcp-status prod
gcp-status stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    echo "Web service: $SERVICE"
    gcloud run services describe "$SERVICE" \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="table(status.url,status.conditions[0].status,spec.template.spec.containerConcurrency)" 2>/dev/null || echo "  (not deployed)"

# Show status of all stages
gcp-status-all:
    @echo "=== DEV ===" && just gcp-status dev 2>/dev/null || echo "(not deployed)"
    @echo ""
    @echo "=== STAGING ===" && just gcp-status staging 2>/dev/null || echo "(not deployed)"
    @echo ""
    @echo "=== PROD ===" && just gcp-status prod 2>/dev/null || echo "(not deployed)"

# Open the web service URL in browser
# Usage: just gcp-open dev | just gcp-open prod
gcp-open stage:
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

# =============================================================================
# GCP Cloud Run - Management Commands
# =============================================================================

# Run database migrations for a stage
# Usage: just gcp-migrate dev | just gcp-migrate prod
gcp-migrate stage:
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
    echo "✓ migrate completed on {{stage}}"

# Run setup_all to initialize database with default data
# Usage: just gcp-setup-data dev | just gcp-setup-data prod
gcp-setup-data stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute environment-specific names
    if [ "{{stage}}" = "prod" ]; then
        JOB_NAME="validibot-setup-all"
        SA="validibot-cloudrun-prod@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db"
        SECRET="django-env"
    else
        JOB_NAME="validibot-setup-all-{{stage}}"
        SA="validibot-cloudrun-{{stage}}@{{gcp_project}}.iam.gserviceaccount.com"
        DB="{{gcp_project}}:{{gcp_region}}:validibot-db-{{stage}}"
        SECRET="django-env-{{stage}}"
    fi

    echo "Running setup_all on {{stage}}..."

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
        --args "-c,set -a && source /secrets/.env && set +a && python manage.py setup_all" \
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
    echo "✓ setup_all completed on {{stage}}"

# Run any Django management command on a deployed environment
# Usage: just gcp-management-cmd prod "seed_plans --force --skip-stripe"
# Usage: just gcp-management-cmd dev "shell"
# Usage: just gcp-management-cmd prod "migrate --check"
gcp-management-cmd stage command:
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

    echo "✓ Command completed on {{stage}}"

# View logs from a Cloud Run job execution
# Usage: just gcp-job-logs validibot-migrate-dev
gcp-job-logs job:
    gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name={{job}}" \
        --project {{gcp_project}} \
        --limit 50 \
        --format="table(timestamp,textPayload)"

# =============================================================================
# GCP Initial Stage Setup
# =============================================================================
#
# Use these commands to set up a new environment (dev, staging, prod) from scratch.
#
# Full setup workflow:
#   1. just gcp-init-stage dev          # Create infrastructure
#   2. just gcp-secrets-init-dev        # Create env file template
#   3. Edit .envs/.dev/.django          # Configure dev-specific values
#   4. just gcp-secrets dev             # Upload secrets
#   5. just gcp-deploy-all dev          # Deploy services
#   6. just gcp-migrate dev             # Run migrations
#   7. just gcp-setup-data dev          # Seed initial data
#
# =============================================================================

# Initialize infrastructure for a new stage (creates service account, database, etc.)
# Usage: just gcp-init-stage dev
# Note: Run this ONCE when setting up a new environment. Idempotent - safe to re-run.
gcp-init-stage stage:
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
        QUEUE_NAME="validibot-validation-queue"
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
        echo "   ✓ Service account already exists"
    else
        gcloud iam service-accounts create "$SA_NAME" \
            --display-name="Validibot {{stage}} Cloud Run" \
            --project={{gcp_project}}
        echo "   ✓ Created"
    fi
    echo ""

    # Step 2: Grant IAM roles to service account
    echo "2. Granting IAM roles to service account"
    ROLES=(
        "roles/cloudsql.client"
        "roles/secretmanager.secretAccessor"
        "roles/storage.objectUser"
        "roles/run.invoker"
        "roles/cloudtasks.enqueuer"
    )
    for role in "${ROLES[@]}"; do
        # Check if binding already exists
        if gcloud projects get-iam-policy {{gcp_project}} \
            --flatten="bindings[].members" \
            --filter="bindings.role=$role AND bindings.members=serviceAccount:$SA_EMAIL" \
            --format="value(bindings.role)" 2>/dev/null | grep -q .; then
            echo "   ✓ $role (already bound)"
        else
            gcloud projects add-iam-policy-binding {{gcp_project}} \
                --member="serviceAccount:$SA_EMAIL" \
                --role="$role" \
                --condition=None \
                --quiet &>/dev/null || true
            echo "   ✓ $role (added)"
        fi
    done
    echo ""

    # Step 3: Create Cloud SQL instance (db-f1-micro for dev, small for staging)
    echo "3. Creating Cloud SQL instance: $DB_INSTANCE"
    if gcloud sql instances describe "$DB_INSTANCE" --project={{gcp_project}} &>/dev/null; then
        echo "   ✓ Database instance already exists"
    else
        TIER="db-f1-micro"
        if [ "{{stage}}" = "staging" ]; then
            TIER="db-g1-small"
        fi
        echo "   Creating $TIER instance (this may take several minutes)..."
        # Note: Uses public IP with IAM-only auth (no IP allowlisting).
        # Cloud Run connects via Cloud SQL Auth Proxy which authenticates via IAM.
        # See docs/dev_docs/google_cloud/security.md for Private IP setup if needed.
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
        echo "   ✓ Created"
    fi
    echo ""

    # Step 4: Create database and user
    echo "4. Creating database and user"
    # Database name is always 'validibot' - isolation is at instance level
    DB_NAME="validibot"
    DB_USER="validibot_user"

    # Check if database exists
    if gcloud sql databases describe "$DB_NAME" --instance="$DB_INSTANCE" --project={{gcp_project}} &>/dev/null; then
        echo "   ✓ Database '$DB_NAME' already exists"
    else
        gcloud sql databases create "$DB_NAME" \
            --instance="$DB_INSTANCE" \
            --project={{gcp_project}}
        echo "   ✓ Database created"
    fi

    # Check if user exists
    if gcloud sql users describe "$DB_USER" --instance="$DB_INSTANCE" --project={{gcp_project}} &>/dev/null; then
        echo "   ✓ User '$DB_USER' already exists"
    else
        # Generate random password
        DB_PASSWORD=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)
        gcloud sql users create "$DB_USER" \
            --instance="$DB_INSTANCE" \
            --password="$DB_PASSWORD" \
            --project={{gcp_project}}
        echo "   ✓ User created"
        echo ""
        echo "   ⚠️  SAVE THIS PASSWORD - it won't be shown again:"
        echo "   Password: $DB_PASSWORD"
        echo ""
        echo "   DATABASE_URL for .envs/.{{stage}}/.django:"
        echo "   postgres://$DB_USER:$DB_PASSWORD@//$DB_NAME?host=/cloudsql/{{gcp_project}}:{{gcp_region}}:$DB_INSTANCE"
    fi
    echo ""

    # Step 5: Create Cloud Tasks queue
    echo "5. Creating Cloud Tasks queue"
    if gcloud tasks queues describe "$QUEUE_NAME" --location={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        echo "   ✓ Queue '$QUEUE_NAME' already exists"
    else
        gcloud tasks queues create "$QUEUE_NAME" \
            --location={{gcp_region}} \
            --project={{gcp_project}}
        echo "   ✓ Queue created"
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
            echo "   ✓ Bucket '$BUCKET' already exists"
        else
            gcloud storage buckets create "gs://$BUCKET" \
                --location={{gcp_region}} \
                --project={{gcp_project}}
            echo "   ✓ Bucket '$BUCKET' created"
        fi
    done
    echo ""

    # Step 7: Grant KMS permissions for credential signing
    echo "7. Granting KMS permissions"
    KMS_KEYRING="validibot-keys"
    # Each stage has its own signing key (prod = credential-signing, others = credential-signing-{stage})
    if [ "{{stage}}" = "prod" ]; then
        KMS_KEY="credential-signing"
    else
        KMS_KEY="credential-signing-{{stage}}"
    fi
    # Check if the key exists
    if gcloud kms keys describe "$KMS_KEY" --location={{gcp_region}} --keyring="$KMS_KEYRING" --project={{gcp_project}} &>/dev/null; then
        # Grant viewer permission (for JWKS endpoint)
        gcloud kms keys add-iam-policy-binding "$KMS_KEY" \
            --location={{gcp_region}} \
            --keyring="$KMS_KEYRING" \
            --project={{gcp_project}} \
            --member="serviceAccount:$SA_EMAIL" \
            --role=roles/cloudkms.viewer \
            --quiet &>/dev/null || true
        # Grant signer permission (for signing credentials)
        gcloud kms keys add-iam-policy-binding "$KMS_KEY" \
            --location={{gcp_region}} \
            --keyring="$KMS_KEYRING" \
            --project={{gcp_project}} \
            --member="serviceAccount:$SA_EMAIL" \
            --role=roles/cloudkms.signerVerifier \
            --quiet &>/dev/null || true
        echo "   ✓ KMS permissions granted for $KMS_KEY"
    else
        echo "   ⚠️  KMS key '$KMS_KEY' not found"
        echo "      Run 'just gcp-kms-setup {{stage}}' first to create the signing key"
    fi
    echo ""

    # Step 8: Create secret placeholder
    echo "8. Creating secret in Secret Manager"
    if gcloud secrets describe "$SECRET_NAME" --project={{gcp_project}} &>/dev/null; then
        echo "   ✓ Secret '$SECRET_NAME' already exists"
    else
        # Create empty secret
        echo "placeholder" | gcloud secrets create "$SECRET_NAME" \
            --replication-policy="user-managed" \
            --locations="{{gcp_region}}" \
            --data-file=- \
            --project={{gcp_project}}
        echo "   ✓ Secret created (placeholder)"
    fi
    echo ""

    # Summary
    echo "============================================="
    echo "✓ Infrastructure setup complete for {{stage}}"
    echo "============================================="
    echo ""
    echo "Resources created:"
    echo "  • Service account: $SA_EMAIL"
    echo "  • Cloud SQL: $DB_INSTANCE (database: $DB_NAME)"
    echo "  • Cloud Tasks queue: $QUEUE_NAME"
    echo "  • GCS buckets: $MEDIA_BUCKET, $FILES_BUCKET"
    echo "  • KMS permissions: cloudkms.viewer, cloudkms.signerVerifier"
    echo "  • Secret: $SECRET_NAME"
    echo ""
    # Determine correct env file path (prod uses .production, others use stage name)
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
    echo "  2. Upload secrets:      just gcp-secrets {{stage}}"
    echo "  3. Deploy services:     just gcp-deploy-all {{stage}}"
    echo "  4. Run migrations:      just gcp-migrate {{stage}}"
    echo "  5. Seed data:           just gcp-setup-data {{stage}}"
    echo "  6. Set up scheduler:    just gcp-scheduler-setup {{stage}}"
    echo "  7. Deploy validators:   just validators-deploy-all {{stage}}"
    echo ""

# List all resources for a stage (useful for verification)
gcp-list-resources stage:
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
    gcloud run services describe "$SERVICE" --region={{gcp_region}} --project={{gcp_project}} --format="value(status.url)" 2>/dev/null && echo "  ✓ $SERVICE" || echo "  ✗ $SERVICE (not deployed)"
    gcloud run services describe "$WORKER" --region={{gcp_region}} --project={{gcp_project}} --format="value(status.url)" 2>/dev/null && echo "  ✓ $WORKER" || echo "  ✗ $WORKER (not deployed)"
    echo ""

    echo "Cloud SQL:"
    gcloud sql instances describe "$DB" --project={{gcp_project}} --format="value(state)" 2>/dev/null && echo "  ✓ $DB" || echo "  ✗ $DB (not found)"
    echo ""

    echo "Secrets:"
    gcloud secrets describe "$SECRET" --project={{gcp_project}} &>/dev/null && echo "  ✓ $SECRET" || echo "  ✗ $SECRET (not found)"
    echo ""

    echo "Service Account:"
    gcloud iam service-accounts describe "$SA@{{gcp_project}}.iam.gserviceaccount.com" --project={{gcp_project}} &>/dev/null && echo "  ✓ $SA" || echo "  ✗ $SA (not found)"

# =============================================================================
# Testing
# =============================================================================

# Run tests against live GCP infrastructure
# Loads env from .envs/.test-on-gcp/.django and runs pytest
# Usage:
#   just test-on-gcp                           # Run all GCP integration tests
#   just test-on-gcp -k "connectivity"         # Run only connectivity tests
#   just test-on-gcp --collect-only            # See which tests would run
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

    # Load env vars line by line to handle special characters like *
    while IFS='=' read -r key value; do
        # Skip empty lines and comments
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        # Export the variable (value may contain special chars)
        export "$key=$value"
    done < "$ENV_FILE"

    echo "Running tests against GCP..."
    echo ""
    uv run pytest tests/tests_integration/ {{args}} -v --log-cli-level=INFO

# Run tests locally (default, no GCP)
test *args:
    uv run pytest {{args}} --log-cli-level=INFO

# =============================================================================
# KMS Setup (Per Stage)
# =============================================================================
#
# Create the KMS keyring and stage-specific signing keys.
# Each stage (dev, staging, prod) gets its own signing key for isolation.
#
# The signing key is used for:
# - JWKS endpoint (/.well-known/jwks.json) - publishes public key
# - Credential signing (validation badges)
#
# Usage:
#   just gcp-kms-setup dev      # Create dev signing key
#   just gcp-kms-setup staging  # Create staging signing key
#   just gcp-kms-setup prod     # Create prod signing key

# Create KMS keyring and signing key for a stage
# Usage: just gcp-kms-setup dev | just gcp-kms-setup prod
gcp-kms-setup stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage parameter
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    echo "🔐 Setting up Google Cloud KMS for {{stage}}"
    echo "======================================"
    echo ""

    KEYRING="validibot-keys"
    # Prod uses base name, others get stage suffix
    if [ "{{stage}}" = "prod" ]; then
        KEYNAME="credential-signing"
    else
        KEYNAME="credential-signing-{{stage}}"
    fi

    # Step 1: Create keyring (idempotent - can't be deleted, shared across stages)
    echo "1. Creating keyring: $KEYRING"
    if gcloud kms keyrings describe "$KEYRING" --location={{gcp_region}} --project={{gcp_project}} &>/dev/null; then
        echo "   ✓ Keyring already exists"
    else
        gcloud kms keyrings create "$KEYRING" \
            --location={{gcp_region}} \
            --project={{gcp_project}}
        echo "   ✓ Keyring created"
    fi
    echo ""

    # Step 2: Create signing key for this stage
    echo "2. Creating signing key: $KEYNAME"
    if gcloud kms keys describe "$KEYNAME" --location={{gcp_region}} --keyring="$KEYRING" --project={{gcp_project}} &>/dev/null; then
        echo "   ✓ Key already exists"
    else
        gcloud kms keys create "$KEYNAME" \
            --location={{gcp_region}} \
            --keyring="$KEYRING" \
            --purpose=asymmetric-signing \
            --default-algorithm=ec-sign-p256-sha256 \
            --protection-level=software \
            --project={{gcp_project}}
        echo "   ✓ Key created (EC P-256, ES256 algorithm)"
    fi
    echo ""

    # Step 3: Show key resource name for secrets
    KEY_PATH="projects/{{gcp_project}}/locations/{{gcp_region}}/keyRings/$KEYRING/cryptoKeys/$KEYNAME"
    echo "============================================="
    echo "✓ KMS setup complete for {{stage}}"
    echo "============================================="
    echo ""
    echo "Add these to your {{stage}} environment secrets:"
    echo ""
    echo "  GCP_KMS_SIGNING_KEY=\"$KEY_PATH\""
    echo "  GCP_KMS_JWKS_KEYS=\"$KEY_PATH\""
    echo "  SV_JWKS_ALG=\"ES256\""
    echo ""
    echo "Next: run 'just gcp-init-stage {{stage}}' to grant KMS permissions"
    echo "to the {{stage}} service account."

# =============================================================================
# Validation & Health Checks (Production)
# =============================================================================
#
# These commands validate production infrastructure. They use the legacy
# gcp_service variable which points to the prod web service.
#
# For stage-specific checks, use: just gcp-status <stage>

# Validate entire GCP setup including KMS, JWKS, and infrastructure (prod only)
validate-all: validate-kms validate-jwks validate-gcp

# Validate Google Cloud KMS setup
validate-kms:
    #!/usr/bin/env bash
    set -euo pipefail

    echo "🔐 Validating Google Cloud KMS Setup"
    echo "======================================"
    echo ""

    TESTS_PASSED=0
    TESTS_FAILED=0

    # Colors
    GREEN='\033[0;32m'
    RED='\033[0;31m'
    NC='\033[0m'

    # Test 1: Check KMS key exists
    echo "1. Checking KMS key exists..."
    if gcloud kms keys describe credential-signing \
        --keyring=validibot-keys \
        --location={{gcp_region}} \
        --project={{gcp_project}} &>/dev/null; then
        echo -e "${GREEN}✓${NC} KMS key 'credential-signing' exists"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗${NC} KMS key 'credential-signing' not found"
        ((TESTS_FAILED++))
    fi
    echo ""

    # Test 2: Check key algorithm
    echo "2. Checking KMS key algorithm..."
    ALGORITHM=$(gcloud kms keys describe credential-signing \
        --keyring=validibot-keys \
        --location={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(versionTemplate.algorithm)")

    if [[ "$ALGORITHM" == "EC_SIGN_P256_SHA256" ]]; then
        echo -e "${GREEN}✓${NC} Correct algorithm: EC_SIGN_P256_SHA256 (for ES256)"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗${NC} Algorithm is $ALGORITHM (expected EC_SIGN_P256_SHA256)"
        ((TESTS_FAILED++))
    fi
    echo ""

    # Test 3: Check service account permissions
    echo "3. Checking service account IAM permissions..."
    IAM_POLICY=$(gcloud kms keys get-iam-policy credential-signing \
        --keyring=validibot-keys \
        --location={{gcp_region}} \
        --project={{gcp_project}} \
        --format=json)

    if echo "$IAM_POLICY" | jq -e ".bindings[] | select(.members[] | contains(\"serviceAccount:{{gcp_sa}}\"))" &>/dev/null; then
        echo -e "${GREEN}✓${NC} Service account has KMS permissions"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗${NC} Service account missing KMS permissions"
        ((TESTS_FAILED++))
    fi
    echo ""

    # Test 4: Check public key can be retrieved
    echo "4. Testing public key retrieval..."
    if gcloud kms keys versions get-public-key 1 \
        --key=credential-signing \
        --keyring=validibot-keys \
        --location={{gcp_region}} \
        --project={{gcp_project}} \
        --output-file=/tmp/kms_public_key.pem &>/dev/null; then
        echo -e "${GREEN}✓${NC} Successfully retrieved public key from KMS"
        ((TESTS_PASSED++))

        # Validate it's a valid EC key
        if openssl ec -pubin -in /tmp/kms_public_key.pem -text -noout &>/dev/null; then
            echo -e "${GREEN}✓${NC} Public key is valid EC P-256"
            ((TESTS_PASSED++))
        else
            echo -e "${RED}✗${NC} Public key format invalid"
            ((TESTS_FAILED++))
        fi
        rm -f /tmp/kms_public_key.pem
    else
        echo -e "${RED}✗${NC} Could not retrieve public key"
        ((TESTS_FAILED++))
    fi
    echo ""

    # Summary
    echo "======================================"
    echo "KMS Validation Summary"
    echo "======================================"
    echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    echo ""

    if [ $TESTS_FAILED -eq 0 ]; then
        echo -e "${GREEN}✓ All KMS tests passed!${NC}"
        exit 0
    else
        echo -e "${RED}✗ Some KMS tests failed${NC}"
        exit 1
    fi

# Validate JWKS endpoint is working correctly
validate-jwks:
    #!/usr/bin/env bash
    set -euo pipefail

    echo "🔑 Validating JWKS Endpoint"
    echo "======================================"
    echo ""

    TESTS_PASSED=0
    TESTS_FAILED=0

    # Colors
    GREEN='\033[0;32m'
    RED='\033[0;31m'
    YELLOW='\033[1;33m'
    NC='\033[0m'

    # Get production URL
    PROD_URL=$(gcloud run services describe {{gcp_service}} \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)")

    echo "Testing JWKS at: ${PROD_URL}/.well-known/jwks.json"
    echo ""

    # Test 1: Endpoint accessible
    echo "1. Checking JWKS endpoint accessibility..."
    JWKS_RESPONSE=$(curl -s "${PROD_URL}/.well-known/jwks.json")
    CURL_STATUS=$?

    if [ $CURL_STATUS -eq 0 ]; then
        echo -e "${GREEN}✓${NC} JWKS endpoint accessible"
        ((TESTS_PASSED++))

        # Test 2: Valid JSON structure
        echo "2. Validating JSON structure..."
        if echo "$JWKS_RESPONSE" | jq -e '.keys' &>/dev/null; then
            echo -e "${GREEN}✓${NC} Valid JWKS structure (has 'keys' array)"
            ((TESTS_PASSED++))

            # Test 3: Key count
            KEY_COUNT=$(echo "$JWKS_RESPONSE" | jq '.keys | length')
            echo "3. Checking key count..."
            if [ "$KEY_COUNT" -gt 0 ]; then
                echo -e "${GREEN}✓${NC} Contains $KEY_COUNT key(s)"
                ((TESTS_PASSED++))

                # Test 4-8: Validate first key structure
                echo "4. Validating first key structure..."
                KTY=$(echo "$JWKS_RESPONSE" | jq -r '.keys[0].kty')
                ALG=$(echo "$JWKS_RESPONSE" | jq -r '.keys[0].alg')
                USE=$(echo "$JWKS_RESPONSE" | jq -r '.keys[0].use')
                KID=$(echo "$JWKS_RESPONSE" | jq -r '.keys[0].kid')

                if [[ "$KTY" == "EC" ]]; then
                    echo -e "${GREEN}✓${NC} Key type: EC"
                    ((TESTS_PASSED++))
                else
                    echo -e "${RED}✗${NC} Key type: $KTY (expected EC)"
                    ((TESTS_FAILED++))
                fi

                if [[ "$ALG" == "ES256" ]]; then
                    echo -e "${GREEN}✓${NC} Algorithm: ES256"
                    ((TESTS_PASSED++))
                else
                    echo -e "${RED}✗${NC} Algorithm: $ALG (expected ES256)"
                    ((TESTS_FAILED++))
                fi

                if [[ "$USE" == "sig" ]]; then
                    echo -e "${GREEN}✓${NC} Use: sig"
                    ((TESTS_PASSED++))
                else
                    echo -e "${RED}✗${NC} Use: $USE (expected sig)"
                    ((TESTS_FAILED++))
                fi

                if [[ -n "$KID" && "$KID" != "null" ]]; then
                    echo -e "${GREEN}✓${NC} Has key ID: ${KID:0:16}..."
                    ((TESTS_PASSED++))
                else
                    echo -e "${RED}✗${NC} Missing key ID"
                    ((TESTS_FAILED++))
                fi

                # Test EC coordinates
                if echo "$JWKS_RESPONSE" | jq -e '.keys[0].x' &>/dev/null && \
                   echo "$JWKS_RESPONSE" | jq -e '.keys[0].y' &>/dev/null; then
                    echo -e "${GREEN}✓${NC} Has EC coordinates (x, y)"
                    ((TESTS_PASSED++))
                else
                    echo -e "${RED}✗${NC} Missing EC coordinates"
                    ((TESTS_FAILED++))
                fi
            else
                echo -e "${RED}✗${NC} No keys in JWKS"
                ((TESTS_FAILED++))
            fi
        else
            echo -e "${RED}✗${NC} Invalid JSON structure"
            ((TESTS_FAILED++))
            echo "Response: $JWKS_RESPONSE"
        fi
    else
        echo -e "${RED}✗${NC} JWKS endpoint not accessible"
        ((TESTS_FAILED++))
    fi
    echo ""

    # Test content type
    echo "5. Checking Content-Type header..."
    CONTENT_TYPE=$(curl -s -I "${PROD_URL}/.well-known/jwks.json" | grep -i "content-type:" | tr -d '\r')
    if echo "$CONTENT_TYPE" | grep -qi "application/jwk-set+json"; then
        echo -e "${GREEN}✓${NC} Correct Content-Type: application/jwk-set+json"
        ((TESTS_PASSED++))
    else
        echo -e "${YELLOW}⚠${NC} Content-Type: $CONTENT_TYPE"
        echo "    (expected application/jwk-set+json)"
    fi
    echo ""

    # Summary
    echo "======================================"
    echo "JWKS Validation Summary"
    echo "======================================"
    echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    echo ""

    if [ $TESTS_FAILED -eq 0 ]; then
        echo -e "${GREEN}✓ All JWKS tests passed!${NC}"
        exit 0
    else
        echo -e "${RED}✗ Some JWKS tests failed${NC}"
        exit 1
    fi

# Validate GCP infrastructure is healthy
validate-gcp:
    #!/usr/bin/env bash
    set -euo pipefail

    echo "☁️  Validating GCP Infrastructure"
    echo "======================================"
    echo ""

    TESTS_PASSED=0
    TESTS_FAILED=0

    # Colors
    GREEN='\033[0;32m'
    RED='\033[0;31m'
    NC='\033[0m'

    # Test 1: Cloud Run service is deployed
    echo "1. Checking Cloud Run service..."
    if gcloud run services describe {{gcp_service}} \
        --region={{gcp_region}} \
        --project={{gcp_project}} &>/dev/null; then
        echo -e "${GREEN}✓${NC} Cloud Run service '{{gcp_service}}' exists"
        ((TESTS_PASSED++))

        # Get service URL
        URL=$(gcloud run services describe {{gcp_service}} \
            --region={{gcp_region}} \
            --project={{gcp_project}} \
            --format="value(status.url)")
        echo "  URL: $URL"
    else
        echo -e "${RED}✗${NC} Cloud Run service not found"
        ((TESTS_FAILED++))
    fi
    echo ""

    # Test 2: Service is healthy (responds to requests)
    echo "2. Checking service health..."
    URL=$(gcloud run services describe {{gcp_service}} \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)")

    if curl -s --max-time 10 "$URL" &>/dev/null; then
        echo -e "${GREEN}✓${NC} Service responds to requests"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗${NC} Service not responding"
        ((TESTS_FAILED++))
    fi
    echo ""

    # Test 3: Check for recent errors in logs
    echo "3. Checking for recent errors in logs..."
    ERROR_COUNT=$(gcloud logging read \
        "resource.type=cloud_run_revision AND resource.labels.service_name={{gcp_service}} AND severity>=ERROR" \
        --project={{gcp_project}} \
        --limit=10 \
        --format=json | jq '. | length')

    if [ "$ERROR_COUNT" -eq 0 ]; then
        echo -e "${GREEN}✓${NC} No recent errors in logs"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗${NC} Found $ERROR_COUNT recent errors"
        ((TESTS_FAILED++))
    fi
    echo ""

    # Test 4: Check Cloud SQL connection
    echo "4. Checking Cloud SQL instance..."
    if gcloud sql instances describe validibot-db \
        --project={{gcp_project}} &>/dev/null; then
        echo -e "${GREEN}✓${NC} Cloud SQL instance exists"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗${NC} Cloud SQL instance not found"
        ((TESTS_FAILED++))
    fi
    echo ""

    # Test 5: Check storage buckets
    echo "5. Checking GCS buckets..."
    BUCKET_COUNT=$(gcloud storage buckets list \
        --project={{gcp_project}} \
        --format=json | jq '. | length')

    if [ "$BUCKET_COUNT" -ge 2 ]; then
        echo -e "${GREEN}✓${NC} Found $BUCKET_COUNT storage buckets"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗${NC} Expected at least 2 buckets, found $BUCKET_COUNT"
        ((TESTS_FAILED++))
    fi
    echo ""

    # Summary
    echo "======================================"
    echo "GCP Validation Summary"
    echo "======================================"
    echo -e "Tests passed: ${GREEN}$TESTS_PASSED${NC}"
    echo -e "Tests failed: ${RED}$TESTS_FAILED${NC}"
    echo ""

    if [ $TESTS_FAILED -eq 0 ]; then
        echo -e "${GREEN}✓ All GCP tests passed!${NC}"
        exit 0
    else
        echo -e "${RED}✗ Some GCP tests failed${NC}"
        exit 1
    fi

# Quick health check - just test if service is responding
# Usage: just health-check dev | just health-check prod
health-check stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    URL=$(gcloud run services describe "$SERVICE" \
        --region={{gcp_region}} \
        --project={{gcp_project}} \
        --format="value(status.url)")
    echo "Checking: $URL"
    curl -s -o /dev/null -w "HTTP Status: %{http_code}\nTime: %{time_total}s\n" "$URL"

# =============================================================================
# Validator Containers (Cloud Run Jobs)
# =============================================================================

# Base Artifact Registry path for validator images
validator_repo := "australia-southeast1-docker.pkg.dev/" + gcp_project + "/validibot"

# Build a specific validator container (energyplus, fmi, etc.)
# Usage: just validator-build energyplus
# Note: Build context is vb_validators_dev/ to include shared core utilities
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
# Usage: just validator-deploy energyplus dev | just validator-deploy fmi prod
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
    echo "✓ $JOB_NAME deployed"

# Build and deploy all validator jobs to a stage
# Usage: just validators-deploy-all dev | just validators-deploy-all prod
validators-deploy-all stage:
    just validator-deploy energyplus {{stage}}
    just validator-deploy fmi {{stage}}

# Run validator container tests locally
validators-test:
    uv run --extra dev pytest vb_validators_dev

# =============================================================================
# Helpers
# =============================================================================

# Connect local Django shell to a Cloud SQL database via proxy
# Uses Cloud SQL Proxy for secure connection.
# Usage: just local-to-gcp-shell dev | just local-to-gcp-shell prod
# Requires: DATABASE_PASSWORD environment variable set
local-to-gcp-shell stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage parameter
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Require password to be set
    if [ -z "${DATABASE_PASSWORD:-}" ]; then
        echo "Error: DATABASE_PASSWORD environment variable not set"
        echo ""
        echo "Usage:"
        echo "  DATABASE_PASSWORD='your-password' just local-to-gcp-shell {{stage}}"
        echo ""
        echo "Get the password from Secret Manager or your .envs/.{{stage}}/.django file"
        exit 1
    fi

    # Compute stage-specific values
    if [ "{{stage}}" = "prod" ]; then
        DB_INSTANCE="{{gcp_project}}:{{gcp_region}}:validibot-db"
    else
        DB_INSTANCE="{{gcp_project}}:{{gcp_region}}:validibot-db-{{stage}}"
    fi

    echo "Starting Cloud SQL Proxy for {{stage}}..."
    cloud-sql-proxy "$DB_INSTANCE" &
    PROXY_PID=$!

    # Give proxy time to start
    sleep 2

    # Set up environment for database via localhost
    export DATABASE_URL="postgres://validibot_user:${DATABASE_PASSWORD}@localhost:5432/validibot"
    export DJANGO_SETTINGS_MODULE="config.settings.local"

    echo ""
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║  Connected to {{stage}} database via Cloud SQL Proxy              ║"
    echo "║  Be careful - changes affect live data!                      ║"
    echo "║  Press Ctrl+D to exit shell, then Ctrl+C to stop proxy       ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo ""

    # Run Django shell
    uv run python manage.py shell || true

    # Clean up proxy
    echo "Stopping Cloud SQL Proxy..."
    kill $PROXY_PID 2>/dev/null || true

# Show the current git SHA that would be used for tagging
show-sha:
    @echo "Current git SHA: {{git_sha}}"
    @echo "Image would be tagged: {{gcp_image}}:{{git_sha}}"

# Authenticate Docker with Google Artifact Registry (run once)
gcp-auth:
    gcloud auth configure-docker australia-southeast1-docker.pkg.dev

# Open the Cloud Run console in browser for a stage
# Usage: just gcp-console dev | just gcp-console prod
gcp-console stage:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi
    if [ "{{stage}}" = "prod" ]; then SERVICE="validibot-web"; else SERVICE="validibot-web-{{stage}}"; fi
    open "https://console.cloud.google.com/run/detail/{{gcp_region}}/$SERVICE/metrics?project={{gcp_project}}"

# Run integration tests end-to-end (starts/stops local Postgres + mailpit)
# Prereqs: Docker Compose available; env from set-env.sh for local settings.
test-integration *args:
    @echo "Starting integration dependencies (postgres, mailpit)..."
    @echo "Ensuring django image (with Chromium & chromedriver) exists..."
    @if [ "${BUILD_DJANGO_IMAGE:-0}" -eq 1 ] || ! docker image inspect validibot-django:latest >/dev/null 2>&1; then \
        docker compose -f docker-compose.local.yml build django; \
    else \
        echo "✓ Reusing existing validibot-django image (set BUILD_DJANGO_IMAGE=1 to force rebuild)"; \
    fi
    docker compose -f docker-compose.local.yml down -v
    docker compose -f docker-compose.local.yml up -d postgres mailpit
    @echo "Running integration tests..."
    docker compose -f docker-compose.local.yml run --rm \
        -e DJANGO_SETTINGS_MODULE=config.settings.test \
        django \
        uv run --extra dev pytest tests/tests_integration/ {{args}} -v --log-cli-level=INFO
    @echo "Stopping integration dependencies..."
    docker compose -f docker-compose.local.yml stop postgres mailpit

# Run E2E tests against deployed staging environment
# Tests the full flow: API -> Cloud Tasks -> Worker -> Cloud Run Job -> Callback
# Requires environment variables (see tests/tests_integration/test_e2e_workflow.py)
test-e2e *args:
    @if [ -z "${E2E_TEST_API_URL:-}" ]; then \
        echo "Error: E2E_TEST_API_URL not set"; \
        echo ""; \
        echo "Usage:"; \
        echo "  E2E_TEST_API_URL=https://your-staging-url.run.app/api/v1 \\"; \
        echo "  E2E_TEST_API_TOKEN=your-api-token \\"; \
        echo "  E2E_TEST_WORKFLOW_ID=workflow-uuid \\"; \
        echo "  just test-e2e"; \
        exit 1; \
    fi
    @echo "Running E2E tests against: ${E2E_TEST_API_URL}"
    uv run --extra dev pytest tests/tests_integration/test_e2e_workflow.py {{args}} -v --log-cli-level=INFO

# =============================================================================
# Cloud Scheduler - Scheduled Task Setup
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
# =============================================================================

gcp_scheduler_timezone := "Australia/Sydney"

# List all Cloud Scheduler jobs for this project
gcp-scheduler-list:
    gcloud scheduler jobs list \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# Set up all scheduled jobs for a stage (dev, staging, prod)
gcp-scheduler-setup stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage parameter
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
        echo "Deploy the worker service first with: just gcp-deploy-worker {{stage}}"
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

        echo "📅 Setting up: $job_name"
        echo "   Schedule: $schedule"
        echo "   Endpoint: $endpoint"

        # Check if job exists
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
        echo "   ✓ Done"
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

    echo "✅ All scheduler jobs configured for {{stage}}!"
    echo ""
    echo "View jobs: just gcp-scheduler-list"
    echo "Run a job manually: just gcp-scheduler-run <job-name>"

# Run a scheduler job manually (useful for testing)
gcp-scheduler-run job_name:
    gcloud scheduler jobs run {{job_name}} \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# Delete all scheduler jobs for a stage (use with caution)
gcp-scheduler-delete-all stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage parameter
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute job suffix
    if [ "{{stage}}" = "prod" ]; then
        JOB_SUFFIX=""
    else
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "⚠️  This will delete ALL scheduler jobs for {{stage}} environment"
    read -p "Are you sure? (y/N) " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        for job_base in validibot-clear-sessions validibot-cleanup-idempotency-keys validibot-cleanup-callback-receipts validibot-purge-expired-submissions validibot-process-purge-retries; do
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

# Pause a scheduler job
gcp-scheduler-pause job_name:
    gcloud scheduler jobs pause {{job_name}} \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# Resume a paused scheduler job
gcp-scheduler-resume job_name:
    gcloud scheduler jobs resume {{job_name}} \
        --project {{gcp_project}} \
        --location {{gcp_region}}

# =============================================================================
# Maintenance Mode
# =============================================================================
#
# Put a stage into maintenance mode to save costs when not in use.
# This pauses/scales down resources without deleting them.
#
# Usage:
#   just gcp-maintenance-on dev    # Put dev into maintenance mode
#   just gcp-maintenance-off dev   # Bring dev back online
#
# What gets paused:
#   - Cloud Run services: Ingress set to 'internal' (blocks public traffic)
#   - Cloud SQL: Instance stopped (takes ~1 min to restart)
#   - Cloud Scheduler: All jobs paused
#   - Cloud Tasks queue: Paused
#
# Note: Cloud Run will still scale to 0 when idle, but maintenance mode
# ensures no traffic can reach the services and stops the database.
# =============================================================================

# Put a stage into maintenance mode (pause all resources)
# Usage: just gcp-maintenance-on dev
gcp-maintenance-on stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage parameter
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Safety check for prod
    if [ "{{stage}}" = "prod" ]; then
        echo "⚠️  WARNING: You are about to put PRODUCTION into maintenance mode!"
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
        QUEUE_NAME="validibot-validation-queue"
        JOB_SUFFIX=""
    else
        WEB_SERVICE="validibot-web-{{stage}}"
        WORKER_SERVICE="validibot-worker-{{stage}}"
        DB_INSTANCE="validibot-db-{{stage}}"
        QUEUE_NAME="validibot-validation-queue-{{stage}}"
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "🔧 Putting {{stage}} into maintenance mode..."
    echo ""

    # 1. Block public traffic to web service
    echo "1. Blocking public traffic to $WEB_SERVICE..."
    gcloud run services update "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --ingress internal \
        --project {{gcp_project}} \
        --quiet
    echo "   ✓ Web service set to internal-only"

    # 2. Worker is already internal-only, but ensure it's set
    echo "2. Confirming $WORKER_SERVICE is internal-only..."
    gcloud run services update "$WORKER_SERVICE" \
        --region {{gcp_region}} \
        --ingress internal \
        --project {{gcp_project}} \
        --quiet 2>/dev/null || echo "   (worker not found or already internal)"
    echo "   ✓ Worker service confirmed internal-only"

    # 3. Pause Cloud Scheduler jobs
    echo "3. Pausing Cloud Scheduler jobs..."
    for job_base in validibot-clear-sessions validibot-cleanup-idempotency-keys validibot-cleanup-callback-receipts; do
        job="${job_base}${JOB_SUFFIX}"
        gcloud scheduler jobs pause "$job" \
            --project {{gcp_project}} \
            --location {{gcp_region}} \
            --quiet 2>/dev/null && echo "   ✓ Paused $job" || echo "   - $job (not found)"
    done

    # 4. Pause Cloud Tasks queue
    echo "4. Pausing Cloud Tasks queue..."
    gcloud tasks queues pause "$QUEUE_NAME" \
        --location {{gcp_region}} \
        --project {{gcp_project}} \
        --quiet 2>/dev/null && echo "   ✓ Queue paused" || echo "   - Queue not found"

    # 5. Stop Cloud SQL instance
    echo "5. Stopping Cloud SQL instance $DB_INSTANCE..."
    echo "   (This saves the most cost but takes ~1 min to restart)"
    gcloud sql instances patch "$DB_INSTANCE" \
        --activation-policy NEVER \
        --project {{gcp_project}} \
        --quiet
    echo "   ✓ Database stopped"

    echo ""
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║  {{stage}} is now in MAINTENANCE MODE                              ║"
    echo "║                                                               ║"
    echo "║  Resources paused (not deleted):                              ║"
    echo "║  • Web service: internal-only (no public traffic)             ║"
    echo "║  • Worker service: internal-only                              ║"
    echo "║  • Scheduler jobs: paused                                     ║"
    echo "║  • Task queue: paused                                         ║"
    echo "║  • Database: stopped                                          ║"
    echo "║                                                               ║"
    echo "║  To bring back online: just gcp-maintenance-off {{stage}}          ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"

# Bring a stage out of maintenance mode (resume all resources)
# Usage: just gcp-maintenance-off dev
gcp-maintenance-off stage:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate stage parameter
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

    # Compute stage-specific names
    if [ "{{stage}}" = "prod" ]; then
        WEB_SERVICE="validibot-web"
        WORKER_SERVICE="validibot-worker"
        DB_INSTANCE="validibot-db"
        QUEUE_NAME="validibot-validation-queue"
        JOB_SUFFIX=""
    else
        WEB_SERVICE="validibot-web-{{stage}}"
        WORKER_SERVICE="validibot-worker-{{stage}}"
        DB_INSTANCE="validibot-db-{{stage}}"
        QUEUE_NAME="validibot-validation-queue-{{stage}}"
        JOB_SUFFIX="-{{stage}}"
    fi

    echo "🚀 Bringing {{stage}} out of maintenance mode..."
    echo ""

    # 1. Start Cloud SQL instance first (takes longest)
    echo "1. Starting Cloud SQL instance $DB_INSTANCE..."
    echo "   (This may take 1-2 minutes)"
    gcloud sql instances patch "$DB_INSTANCE" \
        --activation-policy ALWAYS \
        --project {{gcp_project}} \
        --quiet
    echo "   ✓ Database starting..."

    # 2. Resume Cloud Tasks queue
    echo "2. Resuming Cloud Tasks queue..."
    gcloud tasks queues resume "$QUEUE_NAME" \
        --location {{gcp_region}} \
        --project {{gcp_project}} \
        --quiet 2>/dev/null && echo "   ✓ Queue resumed" || echo "   - Queue not found"

    # 3. Resume Cloud Scheduler jobs
    echo "3. Resuming Cloud Scheduler jobs..."
    for job_base in validibot-clear-sessions validibot-cleanup-idempotency-keys validibot-cleanup-callback-receipts; do
        job="${job_base}${JOB_SUFFIX}"
        gcloud scheduler jobs resume "$job" \
            --project {{gcp_project}} \
            --location {{gcp_region}} \
            --quiet 2>/dev/null && echo "   ✓ Resumed $job" || echo "   - $job (not found)"
    done

    # 4. Restore public traffic to web service
    echo "4. Restoring public traffic to $WEB_SERVICE..."
    gcloud run services update "$WEB_SERVICE" \
        --region {{gcp_region}} \
        --ingress all \
        --project {{gcp_project}} \
        --quiet
    echo "   ✓ Web service accepting public traffic"

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
            echo "   ✓ Database is ready!"
            break
        fi
        echo "   Waiting... ($STATUS)"
        sleep 5
    done

    echo ""
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║  {{stage}} is now ONLINE                                           ║"
    echo "║                                                               ║"
    echo "║  All resources resumed:                                       ║"
    echo "║  • Web service: accepting public traffic                      ║"
    echo "║  • Worker service: internal-only (correct)                    ║"
    echo "║  • Scheduler jobs: running                                    ║"
    echo "║  • Task queue: running                                        ║"
    echo "║  • Database: running                                          ║"
    echo "║                                                               ║"
    echo "║  Check status: just gcp-status {{stage}}                           ║"
    echo "║  Open site: just gcp-open {{stage}}                                ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"

# Check maintenance status for a stage
gcp-maintenance-status stage:
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
        QUEUE_NAME="validibot-validation-queue"
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
        echo "Web service:  ✓ ONLINE (public traffic allowed)"
    else
        echo "Web service:  ⏸ MAINTENANCE ($INGRESS)"
    fi

    # Check database status
    DB_STATUS=$(gcloud sql instances describe "$DB_INSTANCE" \
        --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "unknown")
    if [ "$DB_STATUS" = "RUNNABLE" ]; then
        echo "Database:     ✓ ONLINE ($DB_STATUS)"
    else
        echo "Database:     ⏸ MAINTENANCE ($DB_STATUS)"
    fi

    # Check queue status
    QUEUE_STATUS=$(gcloud tasks queues describe "$QUEUE_NAME" \
        --location {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(state)" 2>/dev/null || echo "unknown")
    if [ "$QUEUE_STATUS" = "RUNNING" ]; then
        echo "Task queue:   ✓ ONLINE ($QUEUE_STATUS)"
    else
        echo "Task queue:   ⏸ MAINTENANCE ($QUEUE_STATUS)"
    fi

    echo ""

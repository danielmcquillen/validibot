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

# GCP Project Settings
gcp_service := "validibot-web"
gcp_project := "project-a509c806-3e21-4fbc-b19"
gcp_region := "australia-southeast1"
gcp_sa := "validibot-cloudrun-prod@" + gcp_project + ".iam.gserviceaccount.com"
gcp_sql := gcp_project + ":" + gcp_region + ":validibot-db"
gcp_image := "australia-southeast1-docker.pkg.dev/" + gcp_project + "/validibot/validibot-web"

# Get git commit hash for image tagging
git_sha := `git rev-parse --short HEAD`

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

# Full deployment: build, push, and deploy to Cloud Run
# Also updates the Cloud Run job to use the new image
gcp-deploy: gcp-build gcp-push
    @echo "Deploying to Cloud Run..."
    gcloud run deploy {{gcp_service}} \
        --image {{gcp_image}}:{{git_sha}} \
        --region {{gcp_region}} \
        --service-account {{gcp_sa}} \
        --add-cloudsql-instances {{gcp_sql}} \
        --set-secrets=/secrets/.env=django-env:latest \
        --min-instances 0 \
        --max-instances 4 \
        --memory 1Gi \
        --allow-unauthenticated \
        --project {{gcp_project}}
    @echo "Note: Cloud Run jobs will use :latest image when next executed"

# =============================================================================
# GCP Cloud Run - Secrets Management
# =============================================================================

# Upload .envs/.production/.django to Secret Manager
# Run this after editing the production environment file
gcp-secrets:
    @echo "Uploading secrets from .envs/.production/.django..."
    gcloud secrets versions add django-env \
        --data-file=.envs/.production/.django \
        --project {{gcp_project}}
    @echo ""
    @echo "✓ Secret updated. Run 'just gcp-deploy' to apply changes."

# =============================================================================
# GCP Cloud Run - Operations
# =============================================================================

# View recent Cloud Run logs (last 50 entries)
gcp-logs:
    gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name={{gcp_service}}" \
        --project {{gcp_project}} \
        --limit 50 \
        --format="table(timestamp,severity,textPayload)"

# View logs and follow (stream new logs as they arrive)
gcp-logs-follow:
    gcloud logging tail "resource.type=cloud_run_revision AND resource.labels.service_name={{gcp_service}}" \
        --project {{gcp_project}} \
        --format="table(timestamp,severity,textPayload)"

# Pause the service (block public access, but keep it deployed)
gcp-pause:
    gcloud run services update {{gcp_service}} \
        --region {{gcp_region}} \
        --ingress internal \
        --project {{gcp_project}}
    @echo "✓ Service paused. Public access blocked."

# Resume the service (restore public access)
gcp-resume:
    gcloud run services update {{gcp_service}} \
        --region {{gcp_region}} \
        --ingress all \
        --project {{gcp_project}}
    @echo "✓ Service resumed. Public access restored."

# Show current service status and URL
gcp-status:
    gcloud run services describe {{gcp_service}} \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="table(status.url,status.conditions[0].status,spec.template.spec.containerConcurrency)"

# =============================================================================
# GCP Cloud Run - Management Commands
# =============================================================================

# Run setup_all to initialize database with default data
gcp-setup-all:
    @echo "Running setup_all on Cloud Run..."
    -gcloud run jobs delete validibot-setup-all --region {{gcp_region}} --project {{gcp_project}} --quiet 2>/dev/null || true
    gcloud run jobs create validibot-setup-all \
        --image {{gcp_image}}:latest \
        --region {{gcp_region}} \
        --service-account {{gcp_sa}} \
        --set-cloudsql-instances {{gcp_sql}} \
        --set-secrets=/secrets/.env=django-env:latest \
        --memory 1Gi \
        --command "/bin/bash" \
        --args "-c,set -a && source /secrets/.env && set +a && python manage.py setup_all" \
        --project {{gcp_project}}
    gcloud run jobs execute validibot-setup-all \
        --region {{gcp_region}} \
        --wait \
        --project {{gcp_project}}
    @echo ""
    @echo "✓ setup_all completed. Check logs with: just gcp-job-logs validibot-setup-all"

# Run database migrations
gcp-migrate:
    @echo "Running migrate on Cloud Run..."
    -gcloud run jobs delete validibot-migrate --region {{gcp_region}} --project {{gcp_project}} --quiet 2>/dev/null || true
    gcloud run jobs create validibot-migrate \
        --image {{gcp_image}}:latest \
        --region {{gcp_region}} \
        --service-account {{gcp_sa}} \
        --set-cloudsql-instances {{gcp_sql}} \
        --set-secrets=/secrets/.env=django-env:latest \
        --memory 1Gi \
        --command "/bin/bash" \
        --args "-c,set -a && source /secrets/.env && set +a && python manage.py migrate --noinput" \
        --project {{gcp_project}}
    gcloud run jobs execute validibot-migrate \
        --region {{gcp_region}} \
        --wait \
        --project {{gcp_project}}
    @echo ""
    @echo "✓ migrate completed. Check logs with: just gcp-job-logs validibot-migrate"

# View logs from a Cloud Run job execution
# Usage: just gcp-job-logs [job-name]
# Examples:
#   just gcp-job-logs validibot-setup-all
#   just gcp-job-logs validibot-migrate
gcp-job-logs job="validibot-setup-all":
    gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name={{job}}" \
        --project {{gcp_project}} \
        --limit 50 \
        --format="table(timestamp,textPayload)"

# =============================================================================
# Helpers
# =============================================================================

# Connect local Django shell to production Cloud SQL database
# This is the recommended way to run arbitrary Django commands against prod.
# Uses Cloud SQL Proxy for secure connection.
# Press Ctrl+D to exit shell, then Ctrl+C to stop proxy
local-to-gcp-shell:
    #!/usr/bin/env bash
    set -euo pipefail
    
    echo "Starting Cloud SQL Proxy in background..."
    cloud-sql-proxy {{gcp_sql}} &
    PROXY_PID=$!
    
    # Give proxy time to start
    sleep 2
    
    # Set up environment for production database via localhost
    export DATABASE_URL="postgres://validibot_user:93BkiwfnpYYbQujc6%2FwPkGzpgcYEPxF2FJzvMIHEJ0Y%3D@localhost:5432/validibot"
    export DJANGO_SETTINGS_MODULE="config.settings.local"
    
    echo ""
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║  Connected to PRODUCTION database via Cloud SQL Proxy        ║"
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

# Open the Cloud Run console in browser
gcp-console:
    open "https://console.cloud.google.com/run/detail/{{gcp_region}}/{{gcp_service}}/metrics?project={{gcp_project}}"

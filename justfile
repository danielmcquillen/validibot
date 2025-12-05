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
gcp_worker_service := "validibot-worker"

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
        --set-env-vars APP_ROLE=web \
        --min-instances 0 \
        --max-instances 4 \
        --memory 1Gi \
        --allow-unauthenticated \
        --project {{gcp_project}}
    @echo "Note: Cloud Run jobs will use :latest image when next executed"

# Deploy worker service (IAM-only, API surface)
gcp-deploy-worker: gcp-build gcp-push
    @echo "Deploying worker to Cloud Run (private)..."
    gcloud run deploy {{gcp_worker_service}} \
        --image {{gcp_image}}:{{git_sha}} \
        --region {{gcp_region}} \
        --service-account {{gcp_sa}} \
        --add-cloudsql-instances {{gcp_sql}} \
        --set-secrets=/secrets/.env=django-env:latest \
        --set-env-vars APP_ROLE=worker \
        --no-allow-unauthenticated \
        --min-instances 0 \
        --max-instances 2 \
        --memory 1Gi \
        --project {{gcp_project}}

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
# Validation & Health Checks
# =============================================================================

# Validate entire GCP setup including KMS, JWKS, and infrastructure
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
health-check:
    #!/usr/bin/env bash
    URL=$(gcloud run services describe {{gcp_service}} \
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
validator-build name:
    docker build --platform linux/amd64 \
        -f vb_validators_dev/{{name}}/Dockerfile \
        -t {{validator_repo}}/validibot-validator-{{name}}:{{git_sha}} \
        -t {{validator_repo}}/validibot-validator-{{name}}:latest \
        vb_validators_dev/{{name}}

# Push a validator container
validator-push name:
    docker push {{validator_repo}}/validibot-validator-{{name}}:{{git_sha}}
    docker push {{validator_repo}}/validibot-validator-{{name}}:latest

# Build and push in one step
validator-build-push name: validator-build name validator-push name

# Deploy a Cloud Run Job for a validator
# Usage: just validator-deploy energyplus
validator-deploy name: validator-build-push name
    gcloud run jobs deploy validibot-validator-{{name}} \
        --image {{validator_repo}}/validibot-validator-{{name}}:{{git_sha}} \
        --region {{gcp_region}} \
        --service-account {{gcp_sa}} \
        --max-retries 0 \
        --task-timeout 3600 \
        --set-env-vars VALIDATOR_VERSION={{git_sha}} \
        --labels validator={{name}},version={{git_sha}} \
        --project {{gcp_project}}

# Build and deploy all validator jobs
validators-deploy-all:
    just validator-deploy energyplus
    just validator-deploy fmi

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

# Open the deployed app URL in browser
gcp-open:
    #!/usr/bin/env bash
    set -euo pipefail
    URL=$(gcloud run services describe {{gcp_service}} \
        --region {{gcp_region}} \
        --project {{gcp_project}} \
        --format="value(status.url)")
    echo "Opening: $URL"
    open "$URL"

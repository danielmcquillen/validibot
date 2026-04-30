# =============================================================================
# Validibot - Self-Hosted Deployment
# =============================================================================
#
# Commands for deploying and managing a self-hosted Validibot instance.
# Self-hosted means a customer-operated single-VM deployment using
# Docker Compose — typically on DigitalOcean, AWS EC2, Hetzner, or
# on-prem. See ADR-2026-04-27 (Boring Self-Hosting and Operator
# Experience) for the full self-hosted operator experience.
#
# This module is the operator-facing entry point. The same recipes are
# mirrored in ``just/gcp/`` for cross-target parity (where applicable);
# the only intentional asymmetry is stage handling — self-hosted is
# single-stage per VM (no stage argument), while GCP is multi-stage.
#
# When to use self-hosted:
#   - Customers running their own copy on DigitalOcean / EC2 / Hetzner
#   - On-premises deployments
#   - Air-gapped or restricted network environments
#   - Data-custody-sensitive evaluations
#
# Architecture:
#   - Docker Compose for container orchestration (docker-compose.production.yml)
#   - PostgreSQL for the database (containerized or external)
#   - Celery + Redis for background tasks
#   - Optional Caddy reverse proxy via the ``caddy`` Compose profile
#     (off by default; bring your own proxy or set COMPOSE_PROFILES=caddy)
#   - Local filesystem or S3-compatible storage for files
#
# Prerequisites:
#   - Docker and Docker Compose installed
#   - Sufficient disk space for database and uploads
#   - SSL certificate (Let's Encrypt via Caddy or your own proxy)
#
# Quick start:
#   1. Copy .envs.example/.production/.self-hosted/.django to .envs/.production/.self-hosted/.django
#   2. Copy .envs.example/.production/.self-hosted/.postgres to .envs/.production/.self-hosted/.postgres
#   3. Optional: copy .envs.example/.production/.self-hosted/.build for Pro/Enterprise
#   4. Edit the environment files with your settings
#   5. just self-hosted check-env
#   6. just self-hosted bootstrap
#
# =============================================================================

# Use bash for shell commands
set shell := ["bash", "-cu"]

# =============================================================================
# Configuration
# =============================================================================

# Docker Compose file for production deployment
# This should be a production-ready compose file (not the local dev one)
compose_file := "docker-compose.production.yml"

# Compose project name (used for container naming)
compose_project := "validibot"

# Environment files for the Docker Compose production deployment
django_env_file := ".envs/.production/.self-hosted/.django"
postgres_env_file := ".envs/.production/.self-hosted/.postgres"
build_env_file := ".envs/.production/.self-hosted/.build"
compose_env_args := `bash -lc 'if [ -f ".envs/.production/.self-hosted/.build" ]; then printf -- "--env-file .envs/.production/.self-hosted/.build"; fi'`

# Git SHA for image tagging
git_sha := `git rev-parse --short HEAD`

# =============================================================================
# Container Lifecycle
# =============================================================================

# Translate the cross-target ENABLE_MCP_SERVER flag into the Compose profile
# that docker-compose.production.yml uses to gate the optional MCP service.
# The recipes below `eval $(just ... _mcp-profile-env)` into their environment
# so the setting is consistent across up/build/build-only.
[private]
_mcp-profile-env:
    #!/usr/bin/env bash
    case "${ENABLE_MCP_SERVER:-false}" in
        true|True|TRUE|1|yes|Yes|YES)
            echo "export COMPOSE_PROFILES=\"\${COMPOSE_PROFILES:+\${COMPOSE_PROFILES},}mcp\""
            ;;
        *)
            ;;
    esac

# Source recipe-level knobs from .envs/.production/.self-hosted/.build
# if present. Kept as a helper so every lifecycle recipe (up, build,
# build-only) picks up the same variables. The file's build-time vars
# (VALIDIBOT_COMMERCIAL_PACKAGE, etc) are separately passed to docker
# compose via ``--env-file`` through ``compose_env_args`` above.
[private]
_load-build-env:
    #!/usr/bin/env bash
    if [ -f .envs/.production/.self-hosted/.build ]; then
        echo "set -a"
        # shellcheck disable=SC1091
        echo "source .envs/.production/.self-hosted/.build"
        echo "set +a"
    fi

# Start all services in production mode.
# Brings up web, worker, database, redis, and any other services. When
# ENABLE_MCP_SERVER=true (set inline or in .envs/.production/
# .self-hosted/.build), also starts the MCP container (profile: mcp).
up:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just self-hosted _load-build-env)"
    eval "$(just self-hosted _mcp-profile-env)"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} up -d

# Stop all services (containers remain, data persists)
# Always acts on every profile so the MCP container is torn down
# regardless of whether it was started via the flag.
down:
    #!/usr/bin/env bash
    set -euo pipefail
    export COMPOSE_PROFILES="${COMPOSE_PROFILES:+${COMPOSE_PROFILES},}mcp"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} down

# Build images and start services.
# Use this after code changes or dependency updates. Honours
# ENABLE_MCP_SERVER=true (inline or from
# .envs/.production/.self-hosted/.build) to include the MCP
# container in the build.
build:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just self-hosted _load-build-env)"
    eval "$(just self-hosted _mcp-profile-env)"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} up -d --build

# Build images without starting (useful for CI/CD).
# Same ENABLE_MCP_SERVER semantics as `build`.
build-only:
    #!/usr/bin/env bash
    set -euo pipefail
    eval "$(just self-hosted _load-build-env)"
    eval "$(just self-hosted _mcp-profile-env)"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} build

# Wait until the web container accepts management commands
_wait-web:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Waiting for web container to become ready..."
    for _ in {1..60}; do
        if docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web true >/dev/null 2>&1; then
            echo "Web container is ready."
            exit 0
        fi
        sleep 2
    done
    echo "Web container did not become ready in time."
    echo "Check logs with: just self-hosted logs-service web"
    exit 1

# First-time install for self-hosted customers
# Builds and starts the stack, applies migrations, seeds default data,
# registers OIDC clients (for MCP OAuth), and verifies the installation.
#
# ensure_oidc_clients is a no-op when no OIDC clients are configured in
# the env file — so this step is safe on community-only deployments
# too. For Pro deployments that have set IDP_OIDC_MCP_SERVER_CLIENT_SECRET,
# this is what creates the Claude Desktop + MCP confidential client
# rows that make OAuth actually work.
bootstrap: check-env build _wait-web
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py migrate --noinput
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py setup_validibot --noinput
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py ensure_oidc_clients
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py check_validibot
    @echo ""
    @echo "Bootstrap complete!"
    @echo "Next steps:"
    @echo "  1. Put Validibot behind your reverse proxy"
    @echo "  2. Visit your configured SITE_URL"
    @echo "  3. Sign in with the superuser credentials from {{django_env_file}}"

# Show status of all containers
status:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps

# Follow logs from all services
logs:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} logs -f

# Follow logs from a specific service
# Usage: just self-hosted logs-service web
logs-service service:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} logs -f {{service}}

# Restart all services
restart: down up

# Restart a specific service
# Usage: just self-hosted restart-service web
restart-service service:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} restart {{service}}

# Stop and remove containers, networks (preserves volumes/data)
clean:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} down

# Stop, remove containers, AND remove volumes
# WARNING: This deletes all data including the database!
clean-all:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "WARNING: This will delete ALL data including the database!"
    read -p "Are you sure? Type 'yes' to confirm: " -r
    if [[ "$REPLY" == "yes" ]]; then
        docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} down -v
        echo "All containers and volumes removed."
    else
        echo "Cancelled."
    fi

# =============================================================================
# Deployment
# =============================================================================

# Full deployment: build and start all services
# This is the main command for deploying updates
deploy: build
    @echo ""
    @echo "Deployment complete!"
    @echo ""
    @echo "Services running:"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps

# Pull latest images (for pre-built images from a registry)
pull:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} pull

# Deploy using pre-built images (pull then start)
deploy-prebuilt: pull up
    @echo "Deployment complete using pre-built images!"

# =============================================================================
# Database & Django Commands
# =============================================================================

# Run database migrations
migrate:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py migrate --noinput

# Run setup_validibot to initialize site and default data
# Configures site domain, validators, roles, workspaces, etc.
setup-data:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py setup_validibot --noinput

# Run any Django management command
# Usage: just self-hosted manage "createsuperuser"
# Usage: just self-hosted manage "shell"
# Usage: just self-hosted manage "collectstatic --noinput"
manage command:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec web python manage.py {{command}}

# Open a bash shell in the web container
shell:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec web bash

# Open Django shell
shell-django:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec web python manage.py shell

# Connect to the PostgreSQL database
shell-db:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec postgres psql -U validibot_user validibot

# =============================================================================
# Backup & Restore
# =============================================================================

# Create a database backup
# Saves to backups/ directory with timestamp
backup-db:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p backups
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="backups/validibot_db_${TIMESTAMP}.sql.gz"
    echo "Creating database backup: $BACKUP_FILE"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T postgres \
        pg_dump -U validibot_user validibot | gzip > "$BACKUP_FILE"
    echo "Backup complete: $BACKUP_FILE"
    ls -lh "$BACKUP_FILE"

# Restore database from a backup file
# Usage: just self-hosted restore-db backups/validibot_db_20240101_120000.sql.gz
restore-db backup_file:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f "{{backup_file}}" ]; then
        echo "Error: Backup file not found: {{backup_file}}"
        exit 1
    fi
    echo "WARNING: This will overwrite the current database!"
    read -p "Are you sure? Type 'yes' to confirm: " -r
    if [[ "$REPLY" == "yes" ]]; then
        echo "Restoring from: {{backup_file}}"
        gunzip -c "{{backup_file}}" | docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T postgres \
            psql -U validibot_user validibot
        echo "Restore complete!"
    else
        echo "Cancelled."
    fi

# List available backups
list-backups:
    @echo "Available backups:"
    @ls -lh backups/*.sql.gz 2>/dev/null || echo "  No backups found in backups/"

# Backup media files
backup-media:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p backups
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_FILE="backups/validibot_media_${TIMESTAMP}.tar.gz"
    echo "Creating media backup: $BACKUP_FILE"
    # Assumes media is in a named volume 'validibot_media'
    docker run --rm \
        -v {{compose_project}}_media:/data:ro \
        -v $(pwd)/backups:/backup \
        alpine tar czf "/backup/validibot_media_${TIMESTAMP}.tar.gz" -C /data .
    echo "Backup complete: $BACKUP_FILE"
    ls -lh "$BACKUP_FILE"

# =============================================================================
# Health Checks
# =============================================================================

# Quick health check - verify services are responding
health-check:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Checking service health..."
    echo ""

    service_status() {
        local service="$1"
        local container_id
        container_id=$(docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps -q "$service" 2>/dev/null || true)
        if [ -z "$container_id" ]; then
            echo "not running"
            return
        fi
        docker inspect --format '{{"{{.State.Status}}"}}' "$container_id" 2>/dev/null || echo "unknown"
    }

    # Check web service
    WEB_STATUS=$(service_status web)
    if [ "$WEB_STATUS" = "running" ]; then
        echo "Web service:    OK (running)"
    else
        echo "Web service:    FAIL ($WEB_STATUS)"
    fi

    # Check worker service
    WORKER_STATUS=$(service_status worker)
    if [ "$WORKER_STATUS" = "running" ]; then
        echo "Worker service: OK (running)"
    else
        echo "Worker service: FAIL ($WORKER_STATUS)"
    fi

    # Check database
    DB_STATUS=$(service_status postgres)
    if [ "$DB_STATUS" = "running" ]; then
        echo "Database:       OK (running)"
    else
        echo "Database:       FAIL ($DB_STATUS)"
    fi

    # Check Redis
    REDIS_STATUS=$(service_status redis)
    if [ "$REDIS_STATUS" = "running" ]; then
        echo "Redis:          OK (running)"
    else
        echo "Redis:          FAIL ($REDIS_STATUS)"
    fi

    echo ""

# Check if the web service is responding to HTTP requests
# Assumes the web service is exposed on localhost:8000 or via reverse proxy
health-check-http:
    #!/usr/bin/env bash
    set -euo pipefail
    # Try localhost:8000 first (direct), then localhost:80 (via proxy)
    for URL in "http://localhost:8000" "http://localhost"; do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$URL" --max-time 5 2>/dev/null || echo "000")
        if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ]; then
            echo "HTTP check: OK ($URL returned $HTTP_CODE)"
            exit 0
        fi
    done
    echo "HTTP check: FAIL (services not responding)"
    exit 1

# =============================================================================
# Maintenance
# =============================================================================

# Enter maintenance mode (stop web, keep database running)
# Useful for database maintenance or major upgrades
maintenance-on:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Entering maintenance mode..."
    echo "Stopping web and worker services..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} stop web worker
    echo ""
    echo "Maintenance mode enabled."
    echo "Database and Redis are still running for maintenance tasks."
    echo ""
    echo "To exit maintenance mode: just self-hosted maintenance-off"

# Exit maintenance mode (restart web and worker)
maintenance-off:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Exiting maintenance mode..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} start web worker
    echo ""
    echo "Services restarted. Maintenance mode disabled."

# =============================================================================
# Updates
# =============================================================================

# Update to latest version
# Pulls new code, rebuilds, and restarts with migrations
update:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Updating Validibot..."
    echo ""
    echo "1. Creating database backup..."
    just self-hosted backup-db
    echo ""
    echo "2. Pulling latest code..."
    if ! git diff --quiet HEAD 2>/dev/null; then
        echo "Error: Working directory has uncommitted changes. Commit or stash them first."
        exit 1
    fi
    git pull --ff-only
    echo ""
    echo "3. Building new images..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} build
    echo ""
    echo "4. Stopping services..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} down
    echo ""
    echo "5. Starting services..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} up -d
    echo ""
    echo "6. Running migrations..."
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec web python manage.py migrate
    echo ""
    echo "Update complete!"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps

# =============================================================================
# SSL / HTTPS Setup
# =============================================================================
#
# For production deployments, you'll need SSL/HTTPS. Options include:
#
# 1. Traefik (recommended for Docker)
#    - Automatic Let's Encrypt certificates
#    - Add Traefik to your docker-compose.production.yml
#    - See: https://doc.traefik.io/traefik/https/acme/
#
# 2. Nginx + Certbot
#    - Traditional approach
#    - Add Nginx container or install on host
#    - Use certbot for Let's Encrypt
#
# 3. Cloudflare Tunnel
#    - No need to expose ports
#    - Free SSL through Cloudflare
#    - Good for home servers
#
# 4. Caddy
#    - Automatic HTTPS by default
#    - Simple configuration
#    - Built-in Let's Encrypt support
#
# Example Traefik labels for docker-compose.production.yml:
#
#   web:
#     labels:
#       - "traefik.enable=true"
#       - "traefik.http.routers.validibot.rule=Host(`validibot.example.com`)"
#       - "traefik.http.routers.validibot.tls.certresolver=letsencrypt"
#       - "traefik.http.services.validibot.loadbalancer.server.port=8000"
#
# =============================================================================

# =============================================================================
# Scaling (Basic)
# =============================================================================

# Scale worker containers
# Usage: just self-hosted scale-workers 3
scale-workers count:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} up -d --scale worker={{count}}
    @echo "Worker count scaled to {{count}}"

# =============================================================================
# Troubleshooting
# =============================================================================

# Show resource usage
resources:
    docker stats --no-stream $(docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps -q)

# Show container logs with errors only
errors:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} logs --tail=100 | grep -i -E "(error|exception|traceback|failed)"

# Show recent container events
events:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} events --since 1h

# Inspect a service's configuration
# Usage: just self-hosted inspect web
inspect service:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} config --services | grep -q {{service}} && \
        docker inspect $(docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} ps -q {{service}}) | jq '.[0]' || \
        echo "Service not found: {{service}}"

# =============================================================================
# Environment Setup
# =============================================================================

# Check if environment file exists and has required variables
check-env:
    #!/usr/bin/env bash
    set -euo pipefail
    DJANGO_ENV_FILE="{{django_env_file}}"
    POSTGRES_ENV_FILE="{{postgres_env_file}}"
    BUILD_ENV_FILE="{{build_env_file}}"

    for env_file in "$DJANGO_ENV_FILE" "$POSTGRES_ENV_FILE"; do
        if [ ! -f "$env_file" ]; then
            echo "ERROR: Environment file not found: $env_file"
            echo ""
            echo "Create it from the examples:"
            echo "  cp .envs.example/.production/.self-hosted/.django $DJANGO_ENV_FILE"
            echo "  cp .envs.example/.production/.self-hosted/.postgres $POSTGRES_ENV_FILE"
            exit 1
        fi
    done

    echo "Checking required Django environment variables..."
    REQUIRED_DJANGO_VARS=(
        "DJANGO_SECRET_KEY"
        "DJANGO_ALLOWED_HOSTS"
        "SITE_URL"
        "WORKER_API_KEY"
    )
    REQUIRED_POSTGRES_VARS=(
        "POSTGRES_HOST"
        "POSTGRES_PORT"
        "POSTGRES_DB"
        "POSTGRES_USER"
        "POSTGRES_PASSWORD"
    )

    MISSING=0
    for var in "${REQUIRED_DJANGO_VARS[@]}"; do
        if ! grep -Eq "^$var=.+" "$DJANGO_ENV_FILE"; then
            echo "  MISSING: $var"
            MISSING=$((MISSING + 1))
        else
            echo "  OK: $var"
        fi
    done

    echo ""
    echo "Checking required PostgreSQL environment variables..."
    for var in "${REQUIRED_POSTGRES_VARS[@]}"; do
        if ! grep -Eq "^$var=.+" "$POSTGRES_ENV_FILE"; then
            echo "  MISSING: $var"
            MISSING=$((MISSING + 1))
        else
            echo "  OK: $var"
        fi
    done

    PLACEHOLDER_MATCHES=0
    for env_file in "$DJANGO_ENV_FILE" "$POSTGRES_ENV_FILE"; do
        if grep -Eq 'change-this|!!!CHANGE-THIS|your-secret-key-here' "$env_file"; then
            echo ""
            echo "ERROR: Placeholder values still exist in $env_file"
            PLACEHOLDER_MATCHES=$((PLACEHOLDER_MATCHES + 1))
        fi
    done

    if [ -f "$BUILD_ENV_FILE" ]; then
        echo ""
        echo "Checking optional Docker build settings..."

        COMMERCIAL_PACKAGE=$(grep -E '^VALIDIBOT_COMMERCIAL_PACKAGE=' "$BUILD_ENV_FILE" | cut -d= -f2- || true)
        PRIVATE_INDEX_URL=$(grep -E '^VALIDIBOT_PRIVATE_INDEX_URL=' "$BUILD_ENV_FILE" | cut -d= -f2- || true)

        if [ -n "$COMMERCIAL_PACKAGE" ]; then
            case "$COMMERCIAL_PACKAGE" in
                validibot-pro|validibot-enterprise)
                    echo "  OK: VALIDIBOT_COMMERCIAL_PACKAGE=$COMMERCIAL_PACKAGE"
                    ;;
                *)
                    echo "  ERROR: Unsupported VALIDIBOT_COMMERCIAL_PACKAGE=$COMMERCIAL_PACKAGE"
                    PLACEHOLDER_MATCHES=$((PLACEHOLDER_MATCHES + 1))
                    ;;
            esac

            if [ -z "$PRIVATE_INDEX_URL" ]; then
                echo "  ERROR: VALIDIBOT_PRIVATE_INDEX_URL is required when installing a commercial package"
                PLACEHOLDER_MATCHES=$((PLACEHOLDER_MATCHES + 1))
            else
                echo "  OK: VALIDIBOT_PRIVATE_INDEX_URL is set"
            fi
        fi
    fi

    echo ""
    if [ "$MISSING" -gt 0 ] || [ "$PLACEHOLDER_MATCHES" -gt 0 ]; then
        echo "Environment validation failed."
        exit 1
    fi

    echo "Environment files look ready."

# Generate a new Django secret key
generate-secret-key:
    @python -c "import secrets; print(secrets.token_urlsafe(50))"

# =============================================================================
# Validators
# =============================================================================
#
# For Docker Compose deployments, validators run as separate Docker containers
# that are invoked by the worker service.
#
# Option 1: Run validators as Docker-in-Docker (DinD)
#   - Worker container has access to Docker socket
#   - Can spawn validator containers dynamically
#
# Option 2: Pre-deploy validator containers
#   - Validators run as always-on services
#   - Worker communicates via HTTP
#
# Option 3: Use an external validator service
#   - Validators hosted elsewhere (cloud, separate server)
#   - Configure VALIDATOR_SERVICE_URL
#
# =============================================================================

# Build validator images locally
# Validator backend source is in the separate validibot-validator-backends repo
validator-build name:
    docker build --platform linux/amd64 \
        -f ../validibot-validator-backends/validator_backends/{{name}}/Dockerfile \
        -t validibot-validator-backend-{{name}}:{{git_sha}} \
        -t validibot-validator-backend-{{name}}:latest \
        ../validibot-validator-backends

# Build all validator images
validators-build-all:
    just self-hosted validator-build energyplus
    just self-hosted validator-build fmu

# =============================================================================
# Operator-Facing Stub Recipes (Phase 0 of ADR-2026-04-27)
# =============================================================================
#
# These recipes are operator-facing entry points with the same names
# they will eventually have. Today they print a "not yet implemented"
# notice and exit non-zero. This makes the operator surface visible
# in ``just self-hosted --list`` and lets cross-target parity tests
# in ``just/gcp/mod.just`` work today.
#
# The full implementations land in later ADR phases:
#   - doctor: Phase 1 (target-aware health diagnostic)
#   - smoke-test: Phase 2 (end-to-end demo workflow)
#   - backup, restore: Phase 3 (application-level backup MVP)
#   - upgrade: Phase 4 (versioned upgrade ritual)
#   - validators (operator surface): Phase 5
#   - collect-support-bundle: Phase 6 (redacted support archive)
#
# Self-hosted is single-stage per VM (see ADR section 4) — these
# recipes do not take a stage argument. The mirrored ``just/gcp/``
# recipes do.

[private]
_phase0-stub recipe:
    #!/usr/bin/env bash
    echo "just self-hosted {{recipe}}: not yet implemented (Phase 0 stub)." >&2
    echo "" >&2
    echo "Implementation lands in a later phase per ADR-2026-04-27" >&2
    echo "(Boring Self-Hosting and Operator Experience). See:" >&2
    echo "  docs/operations/self-hosting/overview.md" >&2
    echo "" >&2
    echo "For now, these existing recipes work today:" >&2
    echo "  just self-hosted bootstrap     # first-time install" >&2
    echo "  just self-hosted deploy        # deploy or upgrade" >&2
    echo "  just self-hosted check-env     # validate env files" >&2
    echo "  just self-hosted check-dns     # verify SITE_URL DNS before TLS" >&2
    echo "  just self-hosted health-check  # quick service health" >&2
    echo "  just self-hosted backup-db     # database-only backup" >&2
    echo "  just self-hosted update        # update with backup + migrations" >&2
    exit 64

# Verify SITE_URL resolves to this host's public IP before TLS issuance.
#
# Caddy requests a Let's Encrypt certificate on first boot via the ACME
# HTTP-01 challenge. If DNS doesn't yet point at this host, the
# challenge fails AND counts against the Let's Encrypt rate limit
# (currently 50 certs per registered domain per week, with stricter
# limits on duplicate failures). Running this once before enabling the
# caddy Compose profile prevents wasted attempts.
#
# Reads SITE_URL from .envs/.production/.self-hosted/.django, resolves
# the hostname via dig, and compares to this host's public IP detected
# via api.ipify.org. Exit codes:
#   0  — DNS resolves to this host. Safe to issue certificates.
#   1  — DNS resolves elsewhere or the env file isn't ready. Fix first.
#   2  — Could not determine this host's public IP. Verify manually.
check-dns:
    #!/usr/bin/env bash
    set -euo pipefail

    ENV_FILE=".envs/.production/.self-hosted/.django"
    if [ ! -f "$ENV_FILE" ]; then
        echo "ERROR: $ENV_FILE not found." >&2
        echo "       Copy it from .envs.example/.production/.self-hosted/.django" >&2
        echo "       and edit SITE_URL before running check-dns." >&2
        exit 1
    fi

    SITE_URL=$(grep -E '^SITE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
    if [ -z "$SITE_URL" ]; then
        echo "ERROR: SITE_URL is empty in $ENV_FILE." >&2
        exit 1
    fi

    # Extract hostname (strip protocol, port, trailing path)
    HOSTNAME=$(echo "$SITE_URL" | sed -E 's|^https?://||; s|/.*||; s|:.*||')
    if [ -z "$HOSTNAME" ]; then
        echo "ERROR: Could not extract hostname from SITE_URL=$SITE_URL." >&2
        exit 1
    fi

    echo "Checking DNS for: $HOSTNAME"

    # Resolve the hostname via dig if available, fall back to host
    if command -v dig >/dev/null 2>&1; then
        RESOLVED_IP=$(dig +short "$HOSTNAME" A | tail -1)
    elif command -v host >/dev/null 2>&1; then
        RESOLVED_IP=$(host -t A "$HOSTNAME" | awk '/has address/ {print $NF; exit}')
    else
        echo "ERROR: Neither dig nor host is installed. Install one and retry." >&2
        exit 1
    fi

    if [ -z "$RESOLVED_IP" ]; then
        echo "FAIL: $HOSTNAME does not resolve to any A record." >&2
        echo "      Add a DNS A-record pointing $HOSTNAME at this host." >&2
        exit 1
    fi
    echo "DNS resolves: $HOSTNAME -> $RESOLVED_IP"

    # Detect this host's public IP via a public service
    PUBLIC_IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo "")
    if [ -z "$PUBLIC_IP" ]; then
        echo "WARN: Could not detect this host's public IP." >&2
        echo "      Manually verify that $HOSTNAME points at this host." >&2
        exit 2
    fi
    echo "Host public IP: $PUBLIC_IP"

    if [ "$RESOLVED_IP" = "$PUBLIC_IP" ]; then
        echo ""
        echo "OK: DNS for $HOSTNAME points at this host."
        echo "    Safe to enable the caddy profile and let it request a TLS cert."
        exit 0
    else
        echo "" >&2
        echo "FAIL: DNS for $HOSTNAME points at $RESOLVED_IP," >&2
        echo "      but this host is at $PUBLIC_IP." >&2
        echo "      Fix DNS before issuing TLS certificates." >&2
        exit 1
    fi

# Build a local Pro image from an exact wheel reference.
#
# Pro images are NOT published to public registries (see ADR section
# 20). Operators always build locally from a wheel they fetched with
# their license credentials. This recipe wraps ``docker build`` with
# BuildKit secrets so the private PyPI credentials never end up in
# image layers, Compose files, logs, or support bundles.
#
# Usage:
#   just self-hosted build-pro-image VALIDIBOT_COMMERCIAL_PACKAGE
#
# where the argument is one of:
#   - validibot-pro==0.1.0 (exact-pinned name+version)
#   - https://...@pypi.validibot.com/.../*.whl#sha256=... (full URL)
#
# Ranges (>=, ~=, ^) are rejected. The recipe reads
# VALIDIBOT_PRIVATE_INDEX_URL from the environment.
#
# Phase 0 status: stub. The wheel-validation logic and BuildKit secret
# wiring land in a later phase.
build-pro-image package="":
    @just self-hosted _phase0-stub build-pro-image

# Health diagnostic — settings, db, cache, storage, validators, signing.
#
# Runs ``python manage.py check_validibot --target self_hosted`` inside
# the web container. Forwards any extra args through to the management
# command, so ``just self-hosted doctor --json --strict`` works as
# expected. See docs/operations/self-hosting/doctor-check-ids.md for
# the table of check IDs and their fixes.
#
# Self-hosted is single-stage per VM (ADR section 4); no stage arg.
doctor *args:
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py check_validibot --target self_hosted {{args}}

# Show recent ERROR / EXCEPTION / TRACEBACK lines from all services.
#
# Useful during incident response. Default window is the last hour;
# pass an explicit duration like ``just self-hosted errors-since 24h``
# or ``just self-hosted errors-since 10m``. Accepts whatever
# ``docker compose logs --since`` accepts.
errors-since duration="1h":
    @echo "Errors / exceptions / tracebacks since {{duration}}:"
    @echo "---"
    docker compose {{compose_env_args}} -f {{compose_file}} -p {{compose_project}} logs --since {{duration}} 2>&1 | grep -i -E "(error|exception|traceback|failed)" || echo "(no errors found)"

# End-to-end smoke test — creates demo data, runs a small validation,
# exports an evidence bundle, marks the demo data clearly.
smoke-test:
    @just self-hosted _phase0-stub smoke-test

# Application-level backup (Postgres dump + DATA_STORAGE_ROOT archive
# + manifest with Validibot version and migration state).
backup:
    @just self-hosted _phase0-stub backup

# Restore from a backup directory. Refuses to overwrite existing data
# without --force.
restore path="":
    @just self-hosted _phase0-stub restore

# Versioned upgrade — backs up, pulls new images, runs migrations,
# restarts services, re-runs doctor and smoke-test, records the event.
upgrade:
    @just self-hosted _phase0-stub upgrade

# Redacted support bundle for paid support sessions. Includes doctor
# output, recent logs, migration state. Excludes secrets, signing
# keys, and raw submission contents.
collect-support-bundle:
    @just self-hosted _phase0-stub collect-support-bundle

# Validator inventory and health — list installed validator images,
# their tags, digests, and smoke-test status.
validators:
    @just self-hosted _phase0-stub validators

# =============================================================================
# Quick Reference
# =============================================================================
#
# Common operations:
#
#   just self-hosted bootstrap     # First-time install
#   just self-hosted deploy        # Deploy/update the application
#   just self-hosted status        # Check if services are running
#   just self-hosted logs          # View logs
#   just self-hosted backup-db     # Create database backup
#   just self-hosted manage "..."  # Run Django management command
#
# First-time setup:
#
#   1. cp .envs.example/.production/.self-hosted/.django .envs/.production/.self-hosted/.django
#   2. cp .envs.example/.production/.self-hosted/.postgres .envs/.production/.self-hosted/.postgres
#   3. Edit both files with your settings
#   4. just self-hosted check-env
#   5. just self-hosted bootstrap
#
# Updating:
#
#   just self-hosted update        # Full update with backup and migrations
#
# Troubleshooting:
#
#   just self-hosted health-check  # Check service status
#   just self-hosted errors        # View recent errors
#   just self-hosted resources     # Check resource usage
#
# =============================================================================

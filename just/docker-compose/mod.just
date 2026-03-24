# =============================================================================
# Validibot - Docker Compose Production Deployment
# =============================================================================
#
# Commands for deploying and managing Validibot using Docker Compose.
# This is the recommended approach for:
#
#   - On-premises deployments
#   - Single-server deployments
#   - Development/staging environments
#   - Air-gapped or restricted network environments
#   - Cost-sensitive deployments (no cloud provider fees)
#
# Architecture:
#   - Docker Compose for container orchestration
#   - PostgreSQL for the database (containerized or external)
#   - Celery + Redis for background tasks
#   - Nginx or Traefik for reverse proxy/SSL termination
#   - Local filesystem or S3-compatible storage for files
#
# Prerequisites:
#   - Docker and Docker Compose installed
#   - Sufficient disk space for database and uploads
#   - SSL certificate (Let's Encrypt recommended)
#
# Quick start:
#   1. Copy .envs.example/.production/.docker-compose/.django to .envs/.production/.docker-compose/.django
#   2. Copy .envs.example/.production/.docker-compose/.postgres to .envs/.production/.docker-compose/.postgres
#   3. Edit the environment files with your settings
#   4. just docker-compose check-env
#   5. just docker-compose bootstrap
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
django_env_file := ".envs/.production/.docker-compose/.django"
postgres_env_file := ".envs/.production/.docker-compose/.postgres"

# Git SHA for image tagging
git_sha := `git rev-parse --short HEAD`

# =============================================================================
# Container Lifecycle
# =============================================================================

# Start all services in production mode
# This brings up web, worker, database, redis, and any other services
up:
    docker compose -f {{compose_file}} -p {{compose_project}} up -d

# Stop all services (containers remain, data persists)
down:
    docker compose -f {{compose_file}} -p {{compose_project}} down

# Build images and start services
# Use this after code changes or dependency updates
build:
    docker compose -f {{compose_file}} -p {{compose_project}} up -d --build

# Build images without starting (useful for CI/CD)
build-only:
    docker compose -f {{compose_file}} -p {{compose_project}} build

# Wait until the web container accepts management commands
_wait-web:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Waiting for web container to become ready..."
    for _ in {1..60}; do
        if docker compose -f {{compose_file}} -p {{compose_project}} exec -T web true >/dev/null 2>&1; then
            echo "Web container is ready."
            exit 0
        fi
        sleep 2
    done
    echo "Web container did not become ready in time."
    echo "Check logs with: just docker-compose logs-service web"
    exit 1

# First-time install for self-hosted customers
# Builds and starts the stack, applies migrations, seeds default data,
# and verifies the installation.
bootstrap: check-env build _wait-web
    docker compose -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py migrate --noinput
    docker compose -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py setup_validibot --noinput
    docker compose -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py check_validibot
    @echo ""
    @echo "Bootstrap complete!"
    @echo "Next steps:"
    @echo "  1. Put Validibot behind your reverse proxy"
    @echo "  2. Visit your configured SITE_URL"
    @echo "  3. Sign in with the superuser credentials from {{django_env_file}}"

# Show status of all containers
status:
    docker compose -f {{compose_file}} -p {{compose_project}} ps

# Follow logs from all services
logs:
    docker compose -f {{compose_file}} -p {{compose_project}} logs -f

# Follow logs from a specific service
# Usage: just docker-compose logs-service web
logs-service service:
    docker compose -f {{compose_file}} -p {{compose_project}} logs -f {{service}}

# Restart all services
restart: down up

# Restart a specific service
# Usage: just docker-compose restart-service web
restart-service service:
    docker compose -f {{compose_file}} -p {{compose_project}} restart {{service}}

# Stop and remove containers, networks (preserves volumes/data)
clean:
    docker compose -f {{compose_file}} -p {{compose_project}} down

# Stop, remove containers, AND remove volumes
# WARNING: This deletes all data including the database!
clean-all:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "WARNING: This will delete ALL data including the database!"
    read -p "Are you sure? Type 'yes' to confirm: " -r
    if [[ "$REPLY" == "yes" ]]; then
        docker compose -f {{compose_file}} -p {{compose_project}} down -v
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
    docker compose -f {{compose_file}} -p {{compose_project}} ps

# Pull latest images (for pre-built images from a registry)
pull:
    docker compose -f {{compose_file}} -p {{compose_project}} pull

# Deploy using pre-built images (pull then start)
deploy-prebuilt: pull up
    @echo "Deployment complete using pre-built images!"

# =============================================================================
# Database & Django Commands
# =============================================================================

# Run database migrations
migrate:
    docker compose -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py migrate --noinput

# Run setup_validibot to initialize site and default data
# Configures site domain, validators, roles, workspaces, etc.
setup-data:
    docker compose -f {{compose_file}} -p {{compose_project}} exec -T web python manage.py setup_validibot --noinput

# Run any Django management command
# Usage: just docker-compose manage "createsuperuser"
# Usage: just docker-compose manage "shell"
# Usage: just docker-compose manage "collectstatic --noinput"
manage command:
    docker compose -f {{compose_file}} -p {{compose_project}} exec web python manage.py {{command}}

# Open a bash shell in the web container
shell:
    docker compose -f {{compose_file}} -p {{compose_project}} exec web bash

# Open Django shell
shell-django:
    docker compose -f {{compose_file}} -p {{compose_project}} exec web python manage.py shell

# Connect to the PostgreSQL database
shell-db:
    docker compose -f {{compose_file}} -p {{compose_project}} exec postgres psql -U validibot_user validibot

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
    docker compose -f {{compose_file}} -p {{compose_project}} exec -T postgres \
        pg_dump -U validibot_user validibot | gzip > "$BACKUP_FILE"
    echo "Backup complete: $BACKUP_FILE"
    ls -lh "$BACKUP_FILE"

# Restore database from a backup file
# Usage: just docker-compose restore-db backups/validibot_db_20240101_120000.sql.gz
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
        gunzip -c "{{backup_file}}" | docker compose -f {{compose_file}} -p {{compose_project}} exec -T postgres \
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
        container_id=$(docker compose -f {{compose_file}} -p {{compose_project}} ps -q "$service" 2>/dev/null || true)
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
    docker compose -f {{compose_file}} -p {{compose_project}} stop web worker
    echo ""
    echo "Maintenance mode enabled."
    echo "Database and Redis are still running for maintenance tasks."
    echo ""
    echo "To exit maintenance mode: just docker-compose maintenance-off"

# Exit maintenance mode (restart web and worker)
maintenance-off:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Exiting maintenance mode..."
    docker compose -f {{compose_file}} -p {{compose_project}} start web worker
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
    just docker-compose backup-db
    echo ""
    echo "2. Pulling latest code..."
    if ! git diff --quiet HEAD 2>/dev/null; then
        echo "Error: Working directory has uncommitted changes. Commit or stash them first."
        exit 1
    fi
    git pull --ff-only
    echo ""
    echo "3. Building new images..."
    docker compose -f {{compose_file}} -p {{compose_project}} build
    echo ""
    echo "4. Stopping services..."
    docker compose -f {{compose_file}} -p {{compose_project}} down
    echo ""
    echo "5. Starting services..."
    docker compose -f {{compose_file}} -p {{compose_project}} up -d
    echo ""
    echo "6. Running migrations..."
    docker compose -f {{compose_file}} -p {{compose_project}} exec web python manage.py migrate
    echo ""
    echo "Update complete!"
    docker compose -f {{compose_file}} -p {{compose_project}} ps

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
# Usage: just docker-compose scale-workers 3
scale-workers count:
    docker compose -f {{compose_file}} -p {{compose_project}} up -d --scale worker={{count}}
    @echo "Worker count scaled to {{count}}"

# =============================================================================
# Troubleshooting
# =============================================================================

# Show resource usage
resources:
    docker stats --no-stream $(docker compose -f {{compose_file}} -p {{compose_project}} ps -q)

# Show container logs with errors only
errors:
    docker compose -f {{compose_file}} -p {{compose_project}} logs --tail=100 | grep -i -E "(error|exception|traceback|failed)"

# Show recent container events
events:
    docker compose -f {{compose_file}} -p {{compose_project}} events --since 1h

# Inspect a service's configuration
# Usage: just docker-compose inspect web
inspect service:
    docker compose -f {{compose_file}} -p {{compose_project}} config --services | grep -q {{service}} && \
        docker inspect $(docker compose -f {{compose_file}} -p {{compose_project}} ps -q {{service}}) | jq '.[0]' || \
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

    for env_file in "$DJANGO_ENV_FILE" "$POSTGRES_ENV_FILE"; do
        if [ ! -f "$env_file" ]; then
            echo "ERROR: Environment file not found: $env_file"
            echo ""
            echo "Create it from the examples:"
            echo "  cp .envs.example/.production/.docker-compose/.django $DJANGO_ENV_FILE"
            echo "  cp .envs.example/.production/.docker-compose/.postgres $POSTGRES_ENV_FILE"
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
# Validator source is in the separate validibot-validators repo
validator-build name:
    docker build --platform linux/amd64 \
        -f ../validibot-validators/validators/{{name}}/Dockerfile \
        -t validibot-validator-{{name}}:{{git_sha}} \
        -t validibot-validator-{{name}}:latest \
        ../validibot-validators

# Build all validator images
validators-build-all:
    just docker-compose validator-build energyplus
    just docker-compose validator-build fmu

# =============================================================================
# Quick Reference
# =============================================================================
#
# Common operations:
#
#   just docker-compose bootstrap     # First-time install
#   just docker-compose deploy        # Deploy/update the application
#   just docker-compose status        # Check if services are running
#   just docker-compose logs          # View logs
#   just docker-compose backup-db     # Create database backup
#   just docker-compose manage "..."  # Run Django management command
#
# First-time setup:
#
#   1. cp .envs.example/.production/.docker-compose/.django .envs/.production/.docker-compose/.django
#   2. cp .envs.example/.production/.docker-compose/.postgres .envs/.production/.docker-compose/.postgres
#   3. Edit both files with your settings
#   4. just docker-compose check-env
#   5. just docker-compose bootstrap
#
# Updating:
#
#   just docker-compose update        # Full update with backup and migrations
#
# Troubleshooting:
#
#   just docker-compose health-check  # Check service status
#   just docker-compose errors        # View recent errors
#   just docker-compose resources     # Check resource usage
#
# =============================================================================

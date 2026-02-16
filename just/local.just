# =============================================================================
# Validibot - Local Development (Docker Compose)
# =============================================================================
#
# Commands for local development using Docker Compose. These run containers
# on your local machine for development and testing.
#
# This is NOT for production Docker Compose deployments - see the 'docker-compose'
# module for that.
#
# Prerequisites:
#   - Docker Desktop or Docker Engine installed
#   - docker-compose.local.yml in the project root
#
# Quick start:
#   just up          # Start all containers
#   just logs        # Follow logs
#   just shell       # Open Django shell
#   just down        # Stop containers
#
# =============================================================================

# Note: Shell setting is inherited from root justfile

# Docker Compose file for local development
local_compose := "docker-compose.local.yml"

# =============================================================================
# Container Lifecycle
# =============================================================================

# Start all local Docker containers in detached mode
# This brings up PostgreSQL, Redis, Django, and any other services defined
# in docker-compose.local.yml
up:
    #!/usr/bin/env bash
    # Check for port conflicts before starting
    conflicts=()
    for port in 8000 8025; do
        pid=$(lsof -ti :$port 2>/dev/null)
        if [ -n "$pid" ]; then
            process=$(ps -p $pid -o comm= 2>/dev/null)
            conflicts+=("  Port $port is in use by '$process' (PID $pid)")
        fi
    done
    if [ ${#conflicts[@]} -gt 0 ]; then
        echo ""
        echo "⚠️  Port conflict detected:"
        for c in "${conflicts[@]}"; do
            echo "$c"
        done
        echo ""
        echo "This is often caused by a leftover process from a previous session."
        echo "To fix: kill the process(es) above, then re-run 'just up'."
        echo ""
        exit 1
    fi
    docker compose -f {{local_compose}} up -d

# Stop all local Docker containers
# Containers are stopped but not removed - data persists in volumes
down:
    docker compose -f {{local_compose}} down

# Rebuild and start all containers
# Use this after changing Dockerfile, requirements, or dependencies
# Usage: just build           # detached (background)
# Usage: just build --attach  # attached (shows logs, Ctrl+C to stop)
build *args:
    docker compose -f {{local_compose}} up {{ if args == "--attach" { "" } else { "-d" } }} --build

# Follow logs from all containers (Ctrl+C to exit)
# Shows interleaved logs from all services
logs:
    docker compose -f {{local_compose}} logs -f

# Show status of all containers
ps:
    docker compose -f {{local_compose}} ps

# Restart all containers (stop then start)
restart: down up

# Stop containers and remove volumes (clean slate)
# WARNING: This deletes all database data! Use for clean slate only.
clean:
    docker compose -f {{local_compose}} down -v --remove-orphans

# =============================================================================
# Django Commands in Container
# =============================================================================

# Helper to construct DATABASE_URL from individual POSTGRES_* env vars
# The entrypoint sets this at container start, but `docker compose exec` bypasses it
_db_url := 'DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"'

# Open a bash shell in the Django container
# Useful for running arbitrary commands, debugging, or exploring
shell:
    docker compose -f {{local_compose}} exec web bash -c '{{_db_url}} exec bash'

# Open a Django shell in the container
# Usage: just djshell
djshell:
    docker compose -f {{local_compose}} exec web bash -c '{{_db_url}} python manage.py shell'

# Run Django migrations in the local container
# Applies any pending database migrations
migrate:
    docker compose -f {{local_compose}} exec web bash -c '{{_db_url}} python manage.py migrate'

# Run a Django management command in the container
# Usage: just manage createsuperuser
# Usage: just manage "shell_plus --ipython"
# Usage: just manage seed_weather_files
manage +command:
    docker compose -f {{local_compose}} exec web bash -c '{{_db_url}} python manage.py {{command}}'

# =============================================================================
# Testing
# =============================================================================

# Run tests locally (no GCP/cloud dependencies)
# Uses local database and mocked services
# Usage: just test
# Usage: just test -k "test_name"    # Run specific test
# Usage: just test --collect-only    # See what tests would run
test *args:
    uv run pytest {{args}} --log-cli-level=INFO

# Run integration tests end-to-end (starts/stops local Postgres + mailpit)
# This spins up isolated containers for testing, separate from dev containers.
# Prereqs: Docker Compose available; BUILD_DJANGO_IMAGE=1 to force image rebuild
test-integration *args:
    @echo "Starting integration dependencies (postgres, mailpit)..."
    @echo "Ensuring web image (with Chromium & chromedriver) exists..."
    @if [ "${BUILD_DJANGO_IMAGE:-0}" -eq 1 ] || ! docker image inspect validibot-web:latest >/dev/null 2>&1; then \
        docker compose -f {{local_compose}} build web; \
    else \
        echo "Reusing existing validibot-web image (set BUILD_DJANGO_IMAGE=1 to force rebuild)"; \
    fi
    docker compose -f {{local_compose}} down -v
    docker compose -f {{local_compose}} up -d postgres mailpit
    @echo "Running integration tests..."
    docker compose -f {{local_compose}} run --rm \
        -e DJANGO_SETTINGS_MODULE=config.settings.test \
        web \
        uv run --extra dev pytest {{ if args == "" { "tests/tests_integration/" } else { args } }} -v --log-cli-level=INFO
    @echo "Stopping integration dependencies..."
    docker compose -f {{local_compose}} stop postgres mailpit

# Run E2E stress tests against a running Docker Compose environment
# Requires: `just up` running. Test data (user, org, workflow, API token) is
# auto-provisioned via the setup_fullstack_test_data management command.
# See: docs/dev_docs/how-to/run-e2e-tests.md
#
# Usage: just test-e2e
test-e2e *args:
    #!/usr/bin/env bash
    set -euo pipefail

    # Auto-provision test data if env vars aren't already set
    if [ -z "${FULLSTACK_API_TOKEN:-}" ]; then
        echo "Provisioning test data via management command..."
        env_output=$(docker compose -f {{local_compose}} exec -T web \
            bash -c '{{_db_url}} python manage.py setup_fullstack_test_data --export-env' 2>/dev/null)
        if [ -z "$env_output" ]; then
            echo ""
            echo "Error: Could not provision test data."
            echo "Make sure 'just up' is running and the app has been set up."
            echo ""
            exit 1
        fi
        eval "$(echo "$env_output")"
        export FULLSTACK_API_TOKEN FULLSTACK_ORG_SLUG FULLSTACK_WORKFLOW_ID
    fi

    echo "Running E2E stress tests against: ${FULLSTACK_API_URL:-http://localhost:8000/api/v1}"
    uv run --extra dev pytest tests/tests_e2e/ {{args}} -v --log-cli-level=INFO

# =============================================================================
# Documentation
# =============================================================================

# Serve developer documentation locally (port 9000)
# For contributors and maintainers
docs-dev:
    uv run mkdocs serve -f mkdocs.dev.yml -a localhost:9000

# Serve user documentation locally (port 9001)
# For end users
docs-user:
    uv run mkdocs serve -f mkdocs.user.yml -a localhost:9001

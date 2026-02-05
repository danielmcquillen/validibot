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

# Open a bash shell in the Django container
# Useful for running arbitrary commands, debugging, or exploring
shell:
    docker compose -f {{local_compose}} exec django bash

# Run Django migrations in the local container
# Applies any pending database migrations
migrate:
    docker compose -f {{local_compose}} exec django python manage.py migrate

# Run a Django management command in the container
# Usage: just manage "createsuperuser"
# Usage: just manage "shell_plus"
manage command:
    docker compose -f {{local_compose}} exec django python manage.py {{command}}

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
    @echo "Ensuring django image (with Chromium & chromedriver) exists..."
    @if [ "${BUILD_DJANGO_IMAGE:-0}" -eq 1 ] || ! docker image inspect validibot-django:latest >/dev/null 2>&1; then \
        docker compose -f {{local_compose}} build django; \
    else \
        echo "Reusing existing validibot-django image (set BUILD_DJANGO_IMAGE=1 to force rebuild)"; \
    fi
    docker compose -f {{local_compose}} down -v
    docker compose -f {{local_compose}} up -d postgres mailpit
    @echo "Running integration tests..."
    docker compose -f {{local_compose}} run --rm \
        -e DJANGO_SETTINGS_MODULE=config.settings.test \
        django \
        uv run --extra dev pytest tests/tests_integration/ {{args}} -v --log-cli-level=INFO
    @echo "Stopping integration dependencies..."
    docker compose -f {{local_compose}} stop postgres mailpit

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

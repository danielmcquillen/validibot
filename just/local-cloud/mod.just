# =============================================================================
# Validibot Cloud — Local Development
# =============================================================================
#
# Run the cloud version locally via Docker Compose.
#
# Usage:
#   just local-cloud up            # Start cloud containers (detached)
#   just local-cloud up --build    # Rebuild and start
#
# To stop:
#   just down                # Works for both local and cloud
#
# =============================================================================

cloud_compose := "../validibot-cloud/docker-compose.cloud.yml"

# Helper to construct DATABASE_URL from individual POSTGRES_* env vars.
# The entrypoint sets this at container start, but `docker compose exec` bypasses it.
# Duplicated from local.just because just modules have isolated scope.
_db_url := 'DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"'

# Start cloud containers locally
[no-cd]
up *args:
    docker compose -f {{cloud_compose}} up -d {{args}}

# Rebuild and start containers, clearing the venv volume
#
# The venv lives in a named Docker volume so it persists across image
# rebuilds.  This recipe removes the volume first, guaranteeing that
# `uv sync` repopulates it from the fresh image (critical after
# dependency upgrades like validibot-shared).
#
# Usage: just local-cloud rebuild              # cached rebuild + fresh venv
#        just local-cloud rebuild --no-cache   # full clean rebuild (slow)
#        just local-cloud rebuild worker       # rebuild specific service
[no-cd]
rebuild *args:
    docker compose -f {{cloud_compose}} down
    -docker volume rm validibot_validibot_local_venv
    docker compose -f {{cloud_compose}} build {{args}}
    docker compose -f {{cloud_compose}} up -d

# Run EnergyPlus E2E tests — one command does everything
#
# This is the single-command entry point for running complete end-to-end
# EnergyPlus tests.  It ensures the stack is running, provisions test
# data, checks for the EnergyPlus Docker image, and runs the tests.
#
# Usage: just local-cloud e2e-tests
#        just local-cloud e2e-tests -k test_passing   # run specific test
[no-cd]
e2e-tests *args:
    #!/usr/bin/env bash
    set -euo pipefail

    compose="{{cloud_compose}}"

    # --- 1. Ensure Docker Compose stack is running ---
    running=$(docker compose -f "$compose" ps --status running -q 2>/dev/null | wc -l | tr -d ' ')
    if [ "$running" -lt 3 ]; then
        echo "Docker Compose stack is not fully running ($running containers)."
        echo "Starting with: docker compose -f $compose up -d --build"
        echo ""
        docker compose -f "$compose" up -d --build
        echo ""
        echo "Waiting for containers to become healthy..."
        sleep 10
    fi

    # --- 2. Check EnergyPlus validator image exists ---
    if ! docker image inspect validibot-validator-energyplus:latest >/dev/null 2>&1; then
        echo ""
        echo "Error: EnergyPlus validator image not found."
        echo "Build it first: (in ../validibot-validators) just build energyplus"
        echo ""
        exit 1
    fi

    # --- 3. Run prerequisites (idempotent) ---
    echo "Ensuring prerequisites (setup_validibot, seed_weather_files)..."
    docker compose -f "$compose" exec -T web \
        bash -c '{{_db_url}} python manage.py setup_validibot' >/dev/null 2>&1
    docker compose -f "$compose" exec -T web \
        bash -c '{{_db_url}} python manage.py seed_weather_files' >/dev/null 2>&1

    # --- 4. Provision E2E workflow data ---
    echo "Provisioning E2E workflow data..."
    env_output=$(docker compose -f "$compose" exec -T web \
        bash -c '{{_db_url}} python manage.py setup_e2e_workflows --export-env' 2>/dev/null)
    if [ -z "$env_output" ]; then
        echo ""
        echo "Error: Could not provision E2E workflow data."
        echo "Check container logs: docker compose -f $compose logs web"
        echo ""
        exit 1
    fi

    # Filter out non-env lines (e.g. Django startup logs) and eval only KEY=VALUE lines
    eval "$(echo "$env_output" | grep -E '^[A-Z_]+=.+$')"
    export FULLSTACK_API_TOKEN FULLSTACK_ORG_SLUG E2E_ENERGYPLUS_TEMPLATE_WORKFLOW_ID

    # --- 5. Run E2E tests ---
    echo ""
    echo "Running EnergyPlus E2E tests against: ${FULLSTACK_API_URL:-http://localhost:8000/api/v1}"
    echo "Workflow ID: ${E2E_ENERGYPLUS_TEMPLATE_WORKFLOW_ID}"
    echo "Note: Each test runs a real EnergyPlus simulation (~2-5 min per test)"
    echo ""
    uv run --group dev pytest tests/tests_e2e/test_energyplus_template.py {{args}} -v --log-cli-level=INFO

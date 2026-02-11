#!/bin/bash
# ==============================================================================
# Host-mode development helper
# ==============================================================================
# This script sets environment variables for running Django directly on your
# machine (without Docker). It sources credentials from your local .envs/ files,
# which are gitignored and must be created by you.
#
# The recommended way to run Validibot locally is with Docker Compose:
#   docker compose up
#
# If you prefer running Django on the host instead, copy the example env files
# and fill in your values:
#   cp -r .envs.example .envs
#   # edit .envs/.local/.django and .envs/.local/.postgres
#   source set-env.sh
# ==============================================================================
#
# Usage:
#   source set-env.sh          # Local Postgres (unix socket)
#   source set-env.sh docker   # Docker Postgres via localhost:5432
#

set -o allexport

if [ ! -f .envs/.local/.django ]; then
    echo "Error: missing .envs/.local/.django (create your local env files first)." >&2
    return 1 2>/dev/null || exit 1
fi

source .envs/.local/.django

if [ -f .envs/.local/.postgres ]; then
    source .envs/.local/.postgres
fi

SET_ENV_MODE="${1:-host}"

if [ "$SET_ENV_MODE" = "docker" ]; then
    # Host-run Django, Docker-managed Postgres.
    # Requires postgres service running and published on localhost.
    POSTGRES_HOST="localhost"
    POSTGRES_PORT="${POSTGRES_PORT:-5432}"
    POSTGRES_DB="${POSTGRES_DB:-validibot}"
    DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
else
    # Host-run Django, local Postgres install (unix socket).
    # Create the database first: createdb validibot
    POSTGRES_HOST="localhost"
    POSTGRES_DB="${POSTGRES_DB:-validibot}"
    DATABASE_URL="postgres:///${POSTGRES_DB}"
fi

unset SET_ENV_MODE
set +o allexport

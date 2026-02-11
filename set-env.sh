#!/bin/bash
# ==============================================================================
# PERSONAL DEVELOPMENT TOOL - NOT PART OF THE COMMUNITY PROJECT
# ==============================================================================
# This script is for running Django directly on the host machine (without Docker).
# It's a personal convenience tool and is gitignored.
#
# The recommended way to run Validibot locally is with Docker Compose:
#   docker compose up
#
# If you want to run Django on your host machine instead, you can create your
# own version of this script or set environment variables manually.
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

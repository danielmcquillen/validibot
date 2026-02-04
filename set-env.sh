# Load local development environment variables for running Django on the host.
#
# This script is designed to be sourced (not executed):
#   source set-env.sh
#
# Usage:
#   source set-env.sh          # Local Postgres (default)
#   source set-env.sh docker   # Docker Postgres via localhost port mapping
#
# Notes:
# - Docker Compose already loads `.envs/.local/.django` and `.envs/.local/.postgres`
#   into containers. This script is for host-run commands like `uv run ...`.
# - `.envs/.local/.django` is written for Docker and usually sets
#   `POSTGRES_HOST=postgres`, which does not resolve on the host.

set -o allexport

if [ ! -f .envs/.local/.django ]; then
    echo "Error: missing .envs/.local/.django (create your local env files first)." >&2
    return 1 2>/dev/null || exit 1
fi

source .envs/.local/.django

# Optional: keep DB credentials in the dedicated file when present.
if [ -f .envs/.local/.postgres ]; then
    source .envs/.local/.postgres
fi

SET_ENV_MODE="${1:-host}"

if [ "$SET_ENV_MODE" = "docker" ]; then
    # Host-run Django, Docker-managed Postgres.
    #
    # Requires `postgres` service running from `docker-compose.local.yml` and
    # published on localhost (default is 5432).
    POSTGRES_HOST="localhost"
    POSTGRES_PORT="${POSTGRES_PORT:-5432}"
    POSTGRES_DB="${POSTGRES_DB:-validibot}"
    DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
else
    # Host-run Django, local Postgres install (unix socket; OS user).
    #
    # If the database doesn't exist yet, create it with:
    #   createdb "${POSTGRES_DB:-validibot}"
    POSTGRES_HOST="localhost"
    POSTGRES_DB="${POSTGRES_DB:-validibot}"
    DATABASE_URL="postgres:///${POSTGRES_DB}"
fi

unset SET_ENV_MODE
set +o allexport

#!/bin/bash
set -o errexit
set -o pipefail
set -o nounset

# Load environment variables from secrets file if it exists (Cloud Run)
if [ -f /secrets/.env ]; then
  echo "Loading environment from /secrets/.env..."
  set -a
  source /secrets/.env
  set +a
fi

# Construct DATABASE_URL from individual postgres variables (if not already set)
# This makes .postgres the single source of truth for credentials
# Skip for Cloud SQL which uses a different connection format
if [ -z "${DATABASE_URL:-}" ] && [ -z "${CLOUD_SQL_CONNECTION_NAME:-}" ]; then
  export DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
fi

# Cloud SQL uses Unix sockets, Docker Compose uses TCP
# Check if we're using Cloud SQL (socket path exists or CLOUD_SQL_CONNECTION_NAME is set)
if [ -n "${CLOUD_SQL_CONNECTION_NAME:-}" ] || [ -d "/cloudsql" ]; then
  echo "Cloud SQL detected, skipping TCP wait (using Unix socket)..."
else
  postgres_host="${POSTGRES_HOST:-postgres}"
  postgres_port="${POSTGRES_PORT:-5432}"
  postgres_user="${POSTGRES_USER:-validibot}"

  echo "Waiting for Postgres at ${postgres_host}:${postgres_port}..."
  until pg_isready -h "${postgres_host}" -p "${postgres_port}" -U "${postgres_user}" >/dev/null 2>&1; do
    sleep 1
  done
fi

# Fix Docker socket permissions if mounted (for spawning validator containers)
# The docker group GID in the container may not match the host socket's GID
if [ -S /var/run/docker.sock ]; then
  DOCKER_SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
  CURRENT_DOCKER_GID=$(getent group docker | cut -d: -f3)
  if [ "${CURRENT_DOCKER_GID}" != "${DOCKER_SOCK_GID}" ]; then
    # Modify docker group to match socket GID
    groupmod -g "${DOCKER_SOCK_GID}" docker 2>/dev/null || true
  fi
fi

# Drop privileges and run command as django user
exec gosu django "$@"

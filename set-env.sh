# Load local development environment variables for running Django outside Docker.
#
# Usage:
#   source set-env.sh            # Host mode (default; good for `uv run ...`)
#   source set-env.sh docker     # Docker mode (keeps docker-compose semantics)
#
# Background:
#   `docker-compose.local.yml` uses `.envs/.local/.django`, which typically sets
#   `POSTGRES_HOST=postgres` (the Docker Compose service name).
#
#   When you run Django directly on the host (outside Docker), that hostname
#   usually won't resolve. In host mode, we default to `POSTGRES_HOST=localhost`
#   and recompute `DATABASE_URL` so local commands can connect to Postgres
#   (either a local install, or the Docker container via `ports: "5432:5432"`).

set -o allexport
source .envs/.local/.django

if [ "${1:-}" != "docker" ] && [ "${POSTGRES_HOST:-}" = "postgres" ]; then
    POSTGRES_HOST="localhost"
    DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
fi

set +o allexport

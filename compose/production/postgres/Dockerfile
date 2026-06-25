# Local/CI Postgres image to mirror production's major version; Cloud SQL provides
# the actual production database, so this image is only used by docker-compose.
# Pinned by digest (OSSF Scorecard Pinned-Dependencies); Dependabot's docker
# ecosystem bumps the digest+tag together so patches still flow.
FROM docker.io/postgres:17-alpine3.19@sha256:d98de4f4959fb6b4531f3fb0cfe92aa49f799e5ebc7a086ae7775b59e7d534e7

# Important Notes

Keep these points in mind during deployment and maintenance.

## Docker Compose

- **Project name must be `validibot`**: The compose files reference a hardcoded volume name (`validibot_validibot_storage`). If you use a different project name via `-p` or `COMPOSE_PROJECT_NAME`, advanced validator containers won't be able to access shared storage. Either use the default project name or update `VALIDATOR_STORAGE_VOLUME` to match. (Network is disabled by default; if you enable `VALIDATOR_NETWORK`, it must also match.)
- **Advanced validator network isolation**: By default, advanced validator containers run with no network access (`network_mode='none'`). This is secure because they communicate via the shared storage volume. If validators need external network access (to download files or call APIs), uncomment `VALIDATOR_NETWORK` in the compose files.
- **Docker socket access**: The worker container mounts `/var/run/docker.sock` to spawn advanced validator containers. This grants root-equivalent access to the host. The entrypoint adjusts group permissions and drops to an unprivileged user, but treat the worker service as privileged.
- **Scheduler singleton**: Only run one scheduler instance. The Beat scheduler doesn't coordinate across replicas, so multiple instances would create duplicate scheduled tasks. Docker Compose naturally runs one, but if you scale via external orchestration, ensure exactly one replica.
- **Health checks**: All services expose `/health/` for container health checks. The endpoint verifies Django is running and the database is reachable.

## Google Cloud Platform

- **Service accounts**: Cloud Run services use stage-specific service accounts (for example `$GCP_APP_NAME-cloudrun-prod@...` and `$GCP_APP_NAME-cloudrun-dev@...`). Ensure they have the necessary IAM roles (Cloud SQL Client, Secret Manager Accessor, Storage Object User, Run Invoker).
- **Secrets**: All secrets are stored in Secret Manager and mounted as `/secrets/.env`. Update with `just gcp secrets`, then redeploy.
- **Cloud SQL connections**: Cloud Run connects to Cloud SQL via the Cloud SQL Auth Proxy (configured via `--add-cloudsql-instances`).
- **Worker vs Web**: The worker service (`APP_ROLE=worker`) handles validator callbacks and scheduled tasks. It is deployed with `--no-allow-unauthenticated`, so Cloud Scheduler, validator Services, retained validator Jobs, and application Cloud Tasks must use Google-signed OIDC tokens. In production, set `SITE_URL=https://your-domain.example` and `WORKER_URL=<worker *.run.app URL>` so internal callbacks and scheduler traffic don't go to the public domain.

## Common Issues

- **Cold starts**: Cloud Run scales to zero by default. First request after idle may take 2-3 seconds. Set `--min-instances=1` if this is a problem.
- **Migrations**: `just gcp deploy-all` runs `check_migration_history` before migrations and refuses databases that still record the deliberately removed pre-2026-07-16 migration tails. This guard is read-only and runs before schema changes. A separate `just gcp migrate` step is normally unnecessary. If you used `GCP_SKIP_MIGRATE=1`, run `just gcp migrate <stage>` before the new code serves traffic; do not bypass a reset-history refusal.
- **Scheduled jobs**: If Cloud Scheduler jobs return 404, verify the worker service is deployed and `APP_ROLE=worker` is set.

## Current-schema reset refusal

The 2026-07-16 pre-launch cleanup replaced several long migration tails without
declaring a Django squash. `python manage.py check_migration_history` detects
databases that recorded those deleted tails. It is read-only and must run
before `migrate`; all managed deployment recipes do this automatically.

If it refuses, do not use `migrate --fake` and do not delete rows from
`django_migrations` to silence it. First preserve a database backup. For a
disposable local/dev database, provision a fresh empty database and let the
normal bootstrap build the current schema. For staging, production, or any
database whose records matter, stop and design an explicit export/rebuild or
one-off bridge migration before changing the database. The guard deliberately
does not automate that destructive decision.

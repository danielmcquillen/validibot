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
- **Worker vs Web**: The worker service (`APP_ROLE=worker`) handles validator callbacks and scheduled tasks. It is deployed with `--no-allow-unauthenticated`, so calls must be authenticated with a Google-signed ID token (Cloud Scheduler, Cloud Run Jobs, and Cloud Tasks if/when we use it). In production, set `SITE_URL=https://validibot.com` and `WORKER_URL=<worker *.run.app URL>` so internal callbacks and scheduler traffic don’t go to the public domain.

## Common Issues

- **Cold starts**: Cloud Run scales to zero by default. First request after idle may take 2-3 seconds. Set `--min-instances=1` if this is a problem.
- **Migrations**: Always run `just gcp migrate` after deploying schema changes.
- **Scheduled jobs**: If Cloud Scheduler jobs return 404, verify the worker service is deployed and `APP_ROLE=worker` is set.

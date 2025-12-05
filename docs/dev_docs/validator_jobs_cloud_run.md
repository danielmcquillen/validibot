# Cloud Run Validator Jobs (web/worker split)

We deploy one Django image as two Cloud Run services:

- **validibot-web** (`APP_ROLE=web`): Public UI + public API.
- **validibot-worker** (`APP_ROLE=worker`): Private/IAM-only internal API (callbacks).

Validator jobs (EnergyPlus, FMI, etc.) run as Cloud Run Jobs and call back to the worker service using Google-signed ID tokens (audience = callback URL). No shared secrets.

## Deployment steps

1) Build/push Django image (same for web/worker)
2) Deploy web:
   - `--allow-unauthenticated`
   - `--set-env-vars APP_ROLE=web`
3) Deploy worker:
   - `--no-allow-unauthenticated`
   - `--set-env-vars APP_ROLE=worker`
   - Grant `roles/run.invoker` on `validibot-worker` to each validator job service account

4) Validator jobs:
   - Tag with labels: `validator=<name>,version=<git_sha>`
   - Env: `VALIDATOR_VERSION=<git_sha>`
   - Callback client mints an ID token via metadata server; Django callback view 404s on non-worker.

## Local vs cloud storage

- Cloud: GCS URIs for envelopes/artifacts.
- Local dev/test: file system paths under `MEDIA_ROOT` (no GCS required).

## Error handling

- Containers log all errors; fatal errors are optionally sent to Sentry if configured.
- User-facing messages stay minimal; detailed context stays in logs/Sentry.
- To inspect logs: open Cloud Logging and filter on `resource.type="cloud_run_job"` and
  `resource.labels.job_name` matching the validator. Fatal errors will include stack traces.
  If Sentry DSN is present in the container, `report_fatal` will forward the exception there.
  (Sentry bootstrap for validator containers is planned; for now, errors always land in Cloud Logging.)

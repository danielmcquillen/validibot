# Important Notes

Keep these points in mind during deployment and maintenance:

## Google Cloud Platform

- **Service accounts**: Cloud Run services use stage-specific service accounts (for example `validibot-cloudrun-prod@...` and `validibot-cloudrun-dev@...`). Ensure they have the necessary IAM roles (Cloud SQL Client, Secret Manager Accessor, Storage Object User, Run Invoker).
- **Secrets**: All secrets are stored in Secret Manager and mounted as `/secrets/.env`. Update with `just gcp-secrets`, then redeploy.
- **Cloud SQL connections**: Cloud Run connects to Cloud SQL via the Cloud SQL Auth Proxy (configured via `--add-cloudsql-instances`).
- **Worker vs Web**: The worker service (`APP_ROLE=worker`) handles validator callbacks and scheduled tasks. It is deployed with `--no-allow-unauthenticated`, so calls must be authenticated with a Google-signed ID token (Cloud Scheduler, Cloud Run Jobs, and Cloud Tasks if/when we use it). In production, set `SITE_URL=https://validibot.com` and `WORKER_URL=<worker *.run.app URL>` so internal callbacks and scheduler traffic don’t go to the public domain.

## JWKS / Badge Signing

- **KMS keys**: Badge signing uses GCP KMS (not AWS). See [kms.md](../google_cloud/kms.md) for setup.
- **JWKS keys**: `SV_JWKS_KEYS` is a comma-separated list for key rotation; if unset it defaults to `[KMS_KEY_ID]`.
- **KMS permissions**: The service account needs `cloudkms.cryptoKeyVersions.viewPublicKey` and `cloudkms.cryptoKeyVersions.useToSign` on the signing key.

## Common Issues

- **Cold starts**: Cloud Run scales to zero by default. First request after idle may take 2-3 seconds. Set `--min-instances=1` if this is a problem.
- **Migrations**: Always run `just gcp-migrate` after deploying schema changes.
- **Scheduled jobs**: If Cloud Scheduler jobs return 404, verify the worker service is deployed and `APP_ROLE=worker` is set.

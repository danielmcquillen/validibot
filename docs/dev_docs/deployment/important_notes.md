# Important Notes

Keep these points in mind during deployment and maintenance:

## Google Cloud Platform

- **Service accounts**: Cloud Run services use the `validibot-cloudrun@...` service account. Ensure it has the necessary IAM roles (Cloud SQL Client, Secret Manager Accessor, Storage Object Admin).
- **Secrets**: All secrets are stored in Secret Manager and mounted as `/secrets/.env`. Update with `just gcp-secrets`, then redeploy.
- **Cloud SQL connections**: Cloud Run connects to Cloud SQL via the Cloud SQL Auth Proxy (configured via `--add-cloudsql-instances`).
- **Worker vs Web**: The worker service (`APP_ROLE=worker`) handles validator callbacks and scheduled tasks. It's not publicly accessible—only Cloud Scheduler and Cloud Tasks can reach it via OIDC authentication.

## JWKS / Badge Signing

- **KMS keys**: Badge signing uses GCP KMS (not AWS). See [kms.md](../google_cloud/kms.md) for setup.
- **JWKS keys**: `SV_JWKS_KEYS` is a comma-separated list for key rotation; if unset it defaults to `[KMS_KEY_ID]`.
- **KMS permissions**: The service account needs `cloudkms.cryptoKeyVersions.viewPublicKey` and `cloudkms.cryptoKeyVersions.useToSign` on the signing key.

## Common Issues

- **Cold starts**: Cloud Run scales to zero by default. First request after idle may take 2-3 seconds. Set `--min-instances=1` if this is a problem.
- **Migrations**: Always run `just gcp-migrate` after deploying schema changes.
- **Scheduled jobs**: If Cloud Scheduler jobs return 404, verify the worker service is deployed and `APP_ROLE=worker` is set.

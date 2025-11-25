# Important Notes

Keep these points in mind during deployment and maintenance:

- **AWS creds for KMS/S3**: Boto3 only reads `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_DEFAULT_REGION` (not the `DJANGO_*` variants). Set them directly in the Heroku config for KMS (JWKS) and any boto3 usage to work.
- **Region consistency**: Ensure the KMS key and S3 buckets live in the region set by `AWS_DEFAULT_REGION` and `DJANGO_AWS_S3_REGION_NAME`.
- **JWKS keys**: `SV_JWKS_KEYS` is a comma-separated list for rotation; if unset it defaults to `[KMS_KEY_ID]`.
- **KMS permissions**: The IAM principal used by the app needs `kms:GetPublicKey` and `kms:Sign` on the signing key (plus key policy allowance).
- **Heroku dyno restarts**: After updating config vars, restart dynos so new credentials/regions are picked up.

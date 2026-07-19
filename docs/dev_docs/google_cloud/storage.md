# Cloud Storage (GCS)

This document covers how Validibot uses Google Cloud Storage for file storage.

> **See also**: [Configuring Storage](../how-to/configure-storage.md) for complete setup instructions.

## Overview

Validibot uses a single GCS bucket with prefix-based separation:

| Prefix | Access | Contents |
|--------|--------|----------|
| `public/` | Public (via IAM condition) | User avatars, workflow images |
| `private/` | Private (service account only) | Validation submissions, artifacts, reports |
| `runs/` | Private (Django + attempt tokens) | Attempt input envelopes, staged inputs, validator outputs |

## Bucket Configuration

### Production

- **Bucket name**: Set via `STORAGE_BUCKET` environment variable
- **Region**: `us-west1`
- **Access control**: Uniform bucket-level access (no per-object ACLs)
- **Public access**: Restricted via IAM to `public/` prefix only

### Development

For local development, you can either:

1. **Use local filesystem** (default) - No configuration needed
2. **Use GCS** - Set `STORAGE_BUCKET` environment variable

## IAM Configuration

The bucket uses IAM Conditions for prefix-based access control:

```bash
# Make public/ prefix publicly readable
gcloud storage buckets add-iam-policy-binding gs://BUCKET \
    --member="allUsers" \
    --role="roles/storage.objectViewer" \
    --condition='expression=resource.name.startsWith("projects/_/buckets/BUCKET/objects/public/"),title=public-prefix-only'

# Django web/worker service account gets full access
gcloud storage buckets add-iam-policy-binding gs://BUCKET \
    --member="serviceAccount:SA@PROJECT.iam.gserviceaccount.com" \
    --role="roles/storage.objectAdmin"
```

Do not grant that role to the validator Cloud Run service account. Validator
jobs receive a short-lived, attempt-prefix Credential Access Boundary token at
dispatch. Run `just gcp validator-storage-capability-probe <stage>` to exercise
the real token against temporary objects, then run
`just gcp validator-storage-isolation <stage>` to remove historical bindings
and prove the runtime identity's effective object permissions are denied.

## Django Configuration

```python
# config/settings/production.py

STORAGE_BUCKET = env("STORAGE_BUCKET")

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": STORAGE_BUCKET,
            "location": "public",  # Django media files go to public/
        },
    },
}

# Validation pipeline files go to private/
DATA_STORAGE_BACKEND = "gcs"
DATA_STORAGE_BUCKET = STORAGE_BUCKET
DATA_STORAGE_PREFIX = "private"
```

## Dependencies

The GCS backend requires `django-storages` with the Google extra:

```toml
# pyproject.toml
"django-storages[google]"
```

This pulls in `google-cloud-storage` automatically.

## Authentication

### Cloud Run

The Django web/worker service uses its attached service account through ADC.
Validator jobs deliberately do not: the launcher injects an explicit short-lived
token limited to one attempt prefix, and the backend refuses an out-of-prefix
URI before making a GCS request.

### Local Development

```bash
# Authenticate with gcloud (one-time setup)
gcloud auth application-default login

# Set bucket and run
export STORAGE_BUCKET=your-dev-bucket
source set-env.sh && uv run python manage.py runserver
```

## Troubleshooting

### "Could not automatically determine credentials"

ADC isn't set up:
- **Locally**: Run `gcloud auth application-default login`
- **Cloud Run**: Ensure service account is attached to the service

### "403 Forbidden" on uploads

Check:
1. Service account has `Storage Object Admin` role on the bucket
2. `STORAGE_BUCKET` environment variable is correct

### Public files returning 403

IAM condition may be misconfigured:
```bash
gcloud storage buckets get-iam-policy gs://BUCKET
```

Verify `allUsers` has `objectViewer` with the correct condition.

### "Need private key to sign credentials"

The service account needs `iam.serviceAccounts.signBlob` permission to generate signed URLs. Either:
- Add the permission to the service account
- Use a service account key file (not recommended for Cloud Run)

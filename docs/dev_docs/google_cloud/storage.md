# Cloud Storage (GCS)

This document covers how Validibot uses Google Cloud Storage for media files, replacing the previous AWS S3 setup.

## Overview

We use Cloud Storage for:

- User-uploaded files (submissions, validation inputs)
- Validation artifacts and reports
- FMU files and related assets

## Buckets

We have two buckets in the `australia-southeast1` region:

| Environment | Bucket Name           | Purpose                       |
| ----------- | --------------------- | ----------------------------- |
| Production  | `validibot-media`     | Production media files        |
| Development | `validibot-media-dev` | Development and staging files |

Both buckets have:

- **Object versioning enabled** - Protection against accidental deletion
- **Private ACL** - No public access by default
- **Lifecycle rules** - Old versions cleaned up automatically

## Django Configuration

We use `django-storages` with the Google Cloud backend. The configuration relies on Application Default Credentials (ADC), meaning no explicit credentials are needed in the code.

### Production Settings

```python
# config/settings/production.py

GCS_MEDIA_BUCKET = env("GCS_MEDIA_BUCKET", default=None)

if not GCS_MEDIA_BUCKET:
    raise Exception("GCS_MEDIA_BUCKET is required in production.")

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": GCS_MEDIA_BUCKET,
            "file_overwrite": False,
        },
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
MEDIA_URL = f"https://storage.googleapis.com/{GCS_MEDIA_BUCKET}/"
```

Key points:

- **No explicit credentials** - Uses ADC (service account on Cloud Run, gcloud auth locally)
- **`file_overwrite: False`** - Prevents accidental overwrites
- **Static files** - Still served via WhiteNoise (not GCS)

### Local Development Settings

Local development defaults to filesystem storage but can optionally use GCS:

```python
# config/settings/local.py

GCS_MEDIA_BUCKET = env("GCS_MEDIA_BUCKET", default=None)

if GCS_MEDIA_BUCKET:
    # Use GCS for media files (matches production)
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
            "OPTIONS": {
                "bucket_name": GCS_MEDIA_BUCKET,
                "file_overwrite": False,
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
    MEDIA_URL = f"https://storage.googleapis.com/{GCS_MEDIA_BUCKET}/"
else:
    # Use local filesystem (default for local development)
    MEDIA_ROOT = BASE_DIR / "media"
    MEDIA_URL = "/media/"
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
```

## Environment Variables

| Variable           | Description            | Example               |
| ------------------ | ---------------------- | --------------------- |
| `GCS_MEDIA_BUCKET` | Name of the GCS bucket | `validibot-media-dev` |

## Dependencies

The GCS backend requires `django-storages` with the Google extra:

```toml
# pyproject.toml
"django-storages[google,s3]==1.14.6"
```

This pulls in `google-cloud-storage` and related dependencies automatically.

## Local Development with GCS

To test GCS locally (optional - filesystem storage works fine for most development):

### 1. Authenticate with gcloud

Run this once to set up Application Default Credentials:

```bash
gcloud auth application-default login
```

This opens a browser for OAuth authentication and stores credentials locally.

### 2. Set Environment Variables

```bash
export GCS_MEDIA_BUCKET=validibot-media-dev
```

### 3. Run Django

```bash
source set-env.sh && uv run python manage.py runserver
```

Uploads will now go to the dev GCS bucket instead of the local `media/` folder.

## Cloud Run Authentication

On Cloud Run, authentication happens automatically via the service account attached to the Cloud Run service. No credentials file or environment variable is needed - `django-storages` uses ADC which detects the Cloud Run environment.

See [IAM & Service Accounts](iam.md) for details on how service accounts are configured.

## Bucket Permissions

Each environment has a dedicated service account with `Storage Object Admin` role on its bucket:

- `validibot-cloudrun-prod@PROJECT.iam.gserviceaccount.com` → `validibot-media`
- `validibot-cloudrun-staging@PROJECT.iam.gserviceaccount.com` → `validibot-media-dev` (future)

This ensures environment isolation - dev can't access prod, and vice versa.

## Migration from S3

The previous AWS S3 configuration used:

- `django-storages[s3]` backend
- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` environment variables
- S3 buckets (not in Australian region)

Key differences:

| Aspect         | S3                                | GCS                                     |
| -------------- | --------------------------------- | --------------------------------------- |
| Authentication | Access key + secret               | Application Default Credentials         |
| Region         | US/EU (Heroku constraint)         | `australia-southeast1`                  |
| Django setting | `S3Boto3Storage`                  | `GoogleCloudStorage`                    |
| URL format     | `https://bucket.s3.amazonaws.com` | `https://storage.googleapis.com/bucket` |

## Troubleshooting

### "Could not automatically determine credentials"

This means ADC isn't set up. Solutions:

- **Locally**: Run `gcloud auth application-default login`
- **Cloud Run**: Ensure a service account is attached to the service

### "403 Forbidden" on uploads

The service account doesn't have permission to the bucket. Check:

1. The correct service account is attached to Cloud Run
2. The service account has `Storage Object Admin` role on the bucket
3. The `GCS_MEDIA_BUCKET` env var matches the bucket you granted access to

### Wrong bucket in uploads

Double-check the `GCS_MEDIA_BUCKET` environment variable in:

- Cloud Run service configuration
- Local `.env` file or shell exports

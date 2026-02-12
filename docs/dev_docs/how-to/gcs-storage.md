# File Storage in Google Cloud

> **Note**: This document provides a quick overview of GCS storage. For complete setup instructions, see [Configuring Storage](configure-storage.md).

## Architecture Overview

Validibot uses a **single GCS bucket** with prefix-based separation:

```
gs://your-storage-bucket/
├── public/      # Publicly accessible (avatars, workflow images)
└── private/     # Private files (validation submissions, artifacts)
```

**How it works:**

1. The bucket itself is **private** (no public access at bucket level)
2. The `public/` prefix is made publicly readable via **IAM Conditions**
3. The `private/` prefix remains accessible only to the service account
4. Users download private files via **time-limited signed URLs**

## Quick Setup

### 1. Create Bucket

```bash
gcloud storage buckets create gs://your-bucket-name \
    --location=us-west1 \
    --uniform-bucket-level-access
```

### 2. Configure IAM

Make `public/` prefix publicly readable:

```bash
gcloud storage buckets add-iam-policy-binding gs://your-bucket-name \
    --member="allUsers" \
    --role="roles/storage.objectViewer" \
    --condition='expression=resource.name.startsWith("projects/_/buckets/your-bucket-name/objects/public/"),title=public-prefix-only'
```

Grant service account full access:

```bash
gcloud storage buckets add-iam-policy-binding gs://your-bucket-name \
    --member="serviceAccount:YOUR_SERVICE_ACCOUNT@PROJECT.iam.gserviceaccount.com" \
    --role="roles/storage.objectAdmin"
```

### 3. Set Environment Variable

```bash
# In production environment
STORAGE_BUCKET=your-bucket-name
```

## Django Configuration

Django's `default` storage uses the `public/` prefix for media files (avatars, workflow images):

```python
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": STORAGE_BUCKET,
            "location": "public",  # Files under public/ prefix
        },
    },
}
```

The Data Storage API uses the `private/` prefix for validation files:

```python
DATA_STORAGE_BACKEND = "gcs"
DATA_STORAGE_BUCKET = STORAGE_BUCKET
DATA_STORAGE_PREFIX = "private"
```

## File Access Patterns

### Public Files (Avatars, Images)

```python
# Direct URL access (no authentication needed)
user.avatar.url
# → https://storage.googleapis.com/bucket/public/avatars/user-123.jpg
```

### Private Files (Validation Data)

```python
from validibot.core.storage import get_data_storage

storage = get_data_storage()

# Generate signed URL for download
url = storage.get_download_url(
    "runs/org-1/run-123/report.pdf",
    expires_in=3600,
)
# → https://storage.googleapis.com/bucket/private/runs/...?X-Goog-Signature=...
```

## Local Development with GCS

To test GCS locally:

```bash
# Authenticate
gcloud auth application-default login

# Set bucket
export STORAGE_BUCKET=your-dev-bucket

# Run Django
source set-env.sh && uv run python manage.py runserver
```

## See Also

- [Configuring Storage](configure-storage.md) - Complete setup guide
- [IAM & Service Accounts](../google_cloud/iam.md) - Service account configuration

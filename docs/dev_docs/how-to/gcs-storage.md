# File Storage in Google Cloud

## Why We Have Two Buckets

Validibot needs to store two different kinds of files:

1. **Public files** like blog post images and user profile pictures that anyone on the internet should be able to view
2. **Private files** like user submissions and validation results that should only be accessible to authenticated users

To handle this cleanly and securely, we use two separate Google Cloud Storage buckets for each environment (production and development). This keeps things simple - public buckets allow public access, private buckets don't. No complicated access control rules needed.

## Bucket Structure

### Production Buckets

| Bucket | Who Can Access | What Goes Here |
|--------|--------|---------|
| `validibot-media` | **Anyone on the internet** | Blog images, workflow featured images, user avatars |
| `validibot-files` | **Only our app** | User submissions, FMU uploads, validation results |

### Development Buckets

Same structure, but with `-dev` suffix for testing:

| Bucket | Who Can Access | What Goes Here |
|--------|--------|---------|
| `validibot-media-dev` | **Anyone on the internet** | Testing public media uploads |
| `validibot-files-dev` | **Only our app** | Testing private file uploads |

## Django Storage Configuration

### Production (`config/settings/production.py`)

```python
STORAGES = {
    # Default storage: Private files bucket
    "default": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": GCS_FILES_BUCKET,  # validibot-files
            "file_overwrite": False,
            "querystring_auth": False,  # Using Compute Engine creds
        },
    },
    # Public storage: Media bucket
    "public": {
        "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
        "OPTIONS": {
            "bucket_name": GCS_MEDIA_BUCKET,  # validibot-media
            "file_overwrite": False,
            "querystring_auth": False,  # Direct public URLs
        },
    },
}
```

### Local Development (`config/settings/local.py`)

By default, uses local filesystem with two directories to mirror production:

```python
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {
            "location": BASE_DIR / "media" / "files",  # Private files
            "base_url": "/media/files/",
        },
    },
    "public": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {
            "location": BASE_DIR / "media" / "public",  # Public media
            "base_url": "/media/public/",
        },
    },
}
```

## Model Field Configuration

### Public Files (Media Bucket)

Use `storage="public"` for files that should be publicly accessible:

```python
class BlogPost(models.Model):
    featured_image = models.FileField(
        null=True,
        blank=True,
        storage="public",  # Uses validibot-media bucket
    )

class User(AbstractUser):
    avatar = models.ImageField(
        upload_to="avatars/",
        storage="public",  # Publicly accessible avatars
    )

class Workflow(models.Model):
    featured_image = models.FileField(
        storage="public",  # Public workflow images
    )
```

### Private Files (Files Bucket)

Use default storage (no `storage=` parameter) for private files:

```python
class Submission(models.Model):
    input_file = models.FileField(
        upload_to="submissions/",
        # No storage= means "default" (private bucket)
    )

class FMU(models.Model):
    file = models.FileField(
        upload_to="fmus/",
        # Private user uploads
    )

class ValidationArtifact(models.Model):
    file = models.FileField(
        upload_to="artifacts/",
        # Private validation outputs
    )
```

## File Access Patterns

### Public Files

Public files in the media bucket can be accessed directly:

```python
# In templates or views
blog_post.featured_image.url
# Returns: https://storage.googleapis.com/validibot-media/blog/image.jpg
```

No authentication required - anyone with the URL can view the file.

### Private Files

Private files in the files bucket require authentication. Currently configured with `querystring_auth=False` because Cloud Run uses Compute Engine credentials (which don't support signing).

**Future Enhancement**: To generate signed URLs for temporary private access, we would need to:
1. Create a service account key file
2. Store it securely in Secret Manager
3. Configure django-storages to use it
4. Set `querystring_auth=True`

## Bucket Permissions

### Media Buckets (Public)

- **IAM Policy**: `allUsers` → `roles/storage.objectViewer`
- **Public Access Prevention**: Disabled (to allow public access)
- **Uniform Bucket-Level Access**: Enabled (no per-object ACLs)

### Files Buckets (Private)

- **IAM Policy**: Service account → `roles/storage.objectAdmin`
- **Public Access Prevention**: Enabled (blocks all public access)
- **Uniform Bucket-Level Access**: Enabled

## Testing with GCS Locally

To test with actual GCS buckets in local development:

1. Authenticate with gcloud:
   ```bash
   gcloud auth application-default login
   ```

2. Set environment variables (either export them in your shell, or add them to `.envs/.local/.django` and re-run `source set-env.sh`):
   ```bash
   export GCS_MEDIA_BUCKET=validibot-media-dev
   export GCS_FILES_BUCKET=validibot-files-dev
   ```

3. Run Django - it will now use GCS instead of local filesystem.

## Adding New File Fields

When adding a new FileField or ImageField to a model:

1. **Determine access level**: Should the file be public or private?

2. **Set storage parameter**:
   - Public files: `storage="public"`
   - Private files: no storage parameter (uses default)

3. **Update the categorization**:
   - Add to the appropriate section in this doc
   - Consider data sovereignty and security requirements

## Monitoring & Costs

- Monitor bucket usage in GCP Console → Storage → Browser
- Set up budget alerts for storage costs
- Review access logs for public buckets if needed
- Consider lifecycle policies for old artifacts

## Troubleshooting

### "Bucket does not exist" error

Check environment variables:
```bash
# In production
gcloud secrets versions access latest --secret=django-env | grep BUCKET

# Locally
echo $GCS_MEDIA_BUCKET
echo $GCS_FILES_BUCKET
```

### "Need private key to sign credentials" error

This means code is trying to generate signed URLs but using Compute Engine credentials. Either:
- Set `querystring_auth=False` in storage OPTIONS
- Or configure a service account key for signing

### Public files not accessible

Check IAM policy:
```bash
gcloud storage buckets get-iam-policy gs://validibot-media
```

Should see `allUsers` with `roles/storage.objectViewer`.

## Security Considerations

1. **Never store sensitive data in public buckets**
   - PII, credentials, private user data → files bucket
   - Marketing content, public images → media bucket

2. **Validate uploads**
   - Check file types and sizes
   - Scan for malware if accepting user uploads

3. **Use consistent patterns**
   - Always use `storage="public"` for public files
   - Never mix public/private data in same bucket

4. **Audit regularly**
   - Review what's in each bucket
   - Clean up unused files
   - Check for inadvertently public files in private bucket

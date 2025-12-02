# IAM & Service Accounts

This document covers how Validibot uses Google Cloud IAM (Identity and Access Management) for secure access to GCP resources.

## Overview

We use a clean separation of service accounts per environment:

- **Dev service account** - Used by dev/staging Cloud Run services
- **Prod service account** - Used by production Cloud Run services

This ensures:

- Environment isolation (dev can't access prod data)
- Principle of least privilege (only permissions needed for that environment)
- No hardcoded credentials in code

## Service Accounts

### Development

| Service Account     | Purpose                            |
| ------------------- | ---------------------------------- |
| `validibot-dev-app` | Runtime identity for dev Cloud Run |

Description: "Validibot dev web/worker runtime SA"

### Production

| Service Account      | Purpose                             |
| -------------------- | ----------------------------------- |
| `validibot-prod-app` | Runtime identity for prod Cloud Run |

Description: "Validibot prod web/worker runtime SA"

## Role Assignments

### Storage Permissions

Each service account needs access only to its environment's bucket:

| Service Account      | Resource                 | Role                 |
| -------------------- | ------------------------ | -------------------- |
| `validibot-dev-app`  | `validibot-au-media-dev` | Storage Object Admin |
| `validibot-prod-app` | `validibot-au-media`     | Storage Object Admin |

`Storage Object Admin` (`roles/storage.objectAdmin`) grants:

- List objects in the bucket
- Read objects
- Create/upload new objects
- Delete objects
- Update object metadata

It does NOT grant:

- Bucket-level administration (creating/deleting buckets)
- IAM policy changes on the bucket
- Access to other buckets

### Future Permissions

As we add more GCP services, these service accounts will need additional roles:

| Service        | Role Needed                    | Purpose                |
| -------------- | ------------------------------ | ---------------------- |
| Cloud SQL      | Cloud SQL Client               | Connect to database    |
| Cloud Tasks    | Cloud Tasks Enqueuer           | Create tasks in queues |
| Secret Manager | Secret Manager Secret Accessor | Read secrets           |

## Creating Service Accounts

### Step 1: Create the Service Account

1. Go to **IAM & Admin → Service Accounts** in the GCP Console
2. Click **➕ Create Service Account**
3. Fill in:
   - **Service account name**: `validibot-dev-app` (or `validibot-prod-app`)
   - **Service account ID**: Auto-fills from name
   - **Description**: "Validibot dev web/worker runtime SA"
4. Click **Create and continue**
5. Skip the "Grant this service account access to project" step (we'll set bucket-level permissions)
6. Click **Done**

The service account email will be:

```
validibot-dev-app@<project-id>.iam.gserviceaccount.com
```

### Step 2: Grant Bucket Access

1. Go to **Cloud Storage → Buckets**
2. Click on your bucket (e.g., `validibot-au-media-dev`)
3. Go to the **Permissions** tab
4. Click **Grant access**
5. In "New principals", paste the service account email
6. For "Role", select **Cloud Storage → Storage Object Admin**
7. Click **Save**

### Step 3: Attach to Cloud Run

1. Go to **Cloud Run → Services**
2. Click on your service (e.g., `validibot-dev-web`)
3. Click **Edit & deploy new revision**
4. Scroll to the **Security** section
5. Under "Service account", select `validibot-dev-app`
6. Click **Deploy**

Repeat for all Cloud Run services in that environment (web and worker).

## Application Default Credentials (ADC)

Our Django application uses [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials) to authenticate with GCP services. This means:

- **No credentials in code** - No JSON key files, no access keys
- **Automatic detection** - Libraries detect the environment and use appropriate credentials
- **Environment-specific** - Uses local user credentials for development, service account for Cloud Run

### How ADC Works

| Environment | Credential Source                       |
| ----------- | --------------------------------------- |
| Local dev   | `gcloud auth application-default login` |
| Cloud Run   | Attached service account (metadata)     |
| Cloud Build | Cloud Build service account             |

### Local Development Setup

To use GCP services locally (optional - local filesystem works for most development):

```bash
# One-time setup - opens browser for OAuth
gcloud auth application-default login
```

This stores credentials at `~/.config/gcloud/application_default_credentials.json`.

The `django-storages` library and other Google Cloud libraries automatically detect and use these credentials.

## Security Best Practices

### Do

- ✅ Use separate service accounts per environment
- ✅ Grant permissions at the resource level (bucket, not project)
- ✅ Use the most specific role possible (Object Admin, not Storage Admin)
- ✅ Rely on ADC instead of key files when possible

### Don't

- ❌ Use the same service account for dev and prod
- ❌ Grant project-wide roles when resource-level roles work
- ❌ Create and download JSON key files unless absolutely necessary
- ❌ Store credentials in code or version control

### If You Must Use a Key File

Sometimes you need a JSON key file (e.g., for CI/CD that doesn't support Workload Identity). If so:

1. Keep the key file out of version control (add to `.gitignore`)
2. Store it securely (encrypted, limited access)
3. Set `GOOGLE_APPLICATION_CREDENTIALS` environment variable to point to it
4. Rotate the key regularly
5. Delete the key when no longer needed

For Cloud Run, **never use key files** - always use the attached service account.

## Troubleshooting

### "Could not automatically determine credentials"

ADC isn't configured. Solutions:

- **Locally**: Run `gcloud auth application-default login`
- **Cloud Run**: Check that a service account is attached to the service

### "Permission denied" errors

The service account doesn't have the required role. Check:

1. The service account is attached to the Cloud Run service
2. The service account has the correct role on the specific resource
3. The role is on the right resource (e.g., the correct bucket)

### Verifying Service Account Permissions

Use the Policy Analyzer to check what a service account can access:

1. Go to **IAM & Admin → Policy Analyzer**
2. Select "Check access"
3. Enter the service account email
4. Specify the resource (e.g., `gs://validibot-au-media-dev`)
5. See what permissions are granted

## Future: Cloud Tasks Authentication

When we add Cloud Tasks, we'll create a separate service account for task invocation:

| Service Account       | Purpose                                  |
| --------------------- | ---------------------------------------- |
| `cloud-tasks-invoker` | OIDC identity for Cloud Tasks HTTP calls |

This service account will have:

- `roles/run.invoker` on the worker Cloud Run service
- No other permissions

Cloud Tasks will use this to call our worker endpoints with a cryptographically signed OIDC token that our Django middleware verifies.

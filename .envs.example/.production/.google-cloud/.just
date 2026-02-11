# Validibot GCP Deployment Configuration (for Just commands)
# ==============================================================================
# Copy this file to .envs/.production/.google-cloud/.just
# Source it before running just commands: source .envs/.production/.google-cloud/.just
#
# These variables are used by the just/gcp/mod.just recipes for deployment.
# They are separate from .django because:
#   1. Just recipes run on the host, not in containers
#   2. These are deployment-time settings, not runtime settings
#   3. The .django file is uploaded to Secret Manager for runtime use

# GCP Project Configuration
# ==============================================================================
# Your GCP project ID (find it in the GCP Console)
export GCP_PROJECT_ID="your-gcp-project-id"

# Region for Cloud Run, Cloud SQL, etc.
# Common options: us-central1, europe-west1, australia-southeast1
export GCP_REGION="us-central1"

# Timezone for Cloud Scheduler cron jobs
# See: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
export GCP_SCHEDULER_TIMEZONE="UTC"

# Cloud Run request timeout (for long-running validator jobs)
# Format: seconds with 's' suffix, max 3600s (1 hour)
export GCP_CLOUD_RUN_TIMEOUT="3600s"

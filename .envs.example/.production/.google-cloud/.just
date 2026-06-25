# Validibot GCP Deployment Configuration (for Just commands)
# ==============================================================================
# Copy this file to .envs/.production/.google-cloud/.just
# Source it before running just commands: source .envs/.production/.google-cloud/.just
#
# WHERE THIS FILE SITS AMONG THE .envs/.../google-cloud/ FILES
# ------------------------------------------------------------
# Four files configure a GCP deployment. They differ by WHO reads them and
# WHEN, and that is what decides where any given variable belongs:
#
#   .just    You `source` it into your shell before running `just`. It is the
#            ONLY file in scope for EVERY deploy recipe — both `just gcp`
#            (web + worker) and `just mcp`. Host-side, deploy-time, not a
#            secret, never uploaded.
#   .django  Uploaded to Secret Manager and mounted into the Django *web*
#            service at runtime. Reaches that ONE service.
#   .mcp     Uploaded as the `mcp-env` secret and mounted into the *MCP*
#            service at runtime. Reaches that ONE service (holds MCP-only
#            runtime config and credentials).
#   .build   Sourced by deploy recipes to supply deploy-time app knobs
#            (commercial build args, ENABLE_MCP_SERVER, MCP API URL, shared
#            x402 values). Values that must reach both web and MCP are
#            authored there once and stamped onto both Cloud Run services.
#
# Rule of thumb:
#   - host-side GCP command context -> here (.just)
#   - Django runtime only          -> .django
#   - MCP runtime only             -> .mcp
#   - deploy/build knob or value shared across services -> .build
#
# (Mechanically: `just` recipes run on the host, not in containers; these are
# deploy-time, not runtime, settings; and .django/.mcp are uploaded to Secret
# Manager for runtime use — so host-sourced values like these stay out of them.)

# GCP Project Configuration
# ==============================================================================
# Your GCP project ID (find it in the GCP Console)
export GCP_PROJECT_ID="your-gcp-project-id"

# Region for Cloud Run, Cloud SQL, etc. (deploy-time, used by `just gcp`).
# Common options: us-central1, europe-west1, australia-southeast1.
# The runtime app also needs its cloud-resource region in .django today because
# many management/job paths mount only the Django secret file. Use the same
# deployment region for both host commands and runtime cloud resources.
export GCP_REGION="your-gcp-region"

# Application name prefix for all GCP resources
# All GCP resource names (Cloud Run services, Cloud SQL instances, buckets,
# service accounts, etc.) are derived from this prefix. For example, with
# GCP_APP_NAME="myapp", resources will be named myapp-web, myapp-db, etc.
# Default: "validibot"
export GCP_APP_NAME="your-gcp-app-name"

# Timezone for Cloud Scheduler cron jobs
# See: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
export GCP_SCHEDULER_TIMEZONE="UTC"

# Cloud Run request timeout (for long-running validator jobs)
# Format: seconds with 's' suffix, max 3600s (1 hour)
export GCP_CLOUD_RUN_TIMEOUT="3600s"

# Commercial Layers (optional)
# ==============================================================================
# Set to "1" to bundle the commercial pro + cloud packages into Docker
# builds. Requires the corresponding sibling directories to exist and is
# only meaningful if you have a license for them. When unset or "0",
# `just gcp build` produces a community-only image.
# export GCP_BUILD_VALIDIBOT_CLOUD="1"

# =============================================================================
# Validibot - Shared Configuration & Helpers
# =============================================================================
#
# This file contains shared variables and helper functions used across all
# deployment platforms (GCP, AWS, self-hosted). It is imported into the root
# justfile and available to all modules.
#
# =============================================================================

# =============================================================================
# Shared Variables
# =============================================================================

# Git SHA for image tagging - used by all platforms for versioning
# This ensures we can trace deployed images back to specific commits
git_sha := `git rev-parse --short HEAD`

# =============================================================================
# Stage Validation
# =============================================================================
#
# All platforms support three deployment stages:
#   - dev:     Development environment (cost-optimized, scales to zero)
#   - staging: Pre-production testing (mirrors prod config, scales to zero)
#   - prod:    Production environment (always-on, higher resources)
#
# Each stage has isolated:
#   - Database instances
#   - Storage buckets/volumes
#   - Secrets/environment variables
#   - Service accounts/IAM roles
#
# =============================================================================

# Helper to validate stage parameter
# Usage: just _validate-stage dev
[private]
[no-cd]
_validate-stage stage:
    #!/usr/bin/env bash
    if [[ ! "{{stage}}" =~ ^(dev|staging|prod)$ ]]; then
        echo "Error: stage must be 'dev', 'staging', or 'prod'"
        exit 1
    fi

# =============================================================================
# Resource Naming Conventions
# =============================================================================
#
# All platforms follow consistent naming for resources:
#
# Production (stage=prod):
#   - Services:  validibot-web, validibot-worker
#   - Database:  validibot-db
#   - Secrets:   django-env
#   - Buckets:   validibot-media, validibot-files
#
# Non-production (stage=dev|staging):
#   - Services:  validibot-web-{stage}, validibot-worker-{stage}
#   - Database:  validibot-db-{stage}
#   - Secrets:   django-env-{stage}
#   - Buckets:   validibot-media-{stage}, validibot-files-{stage}
#
# This convention ensures:
#   1. Production resources have clean names (no suffix)
#   2. Non-prod resources are clearly labeled
#   3. All platforms use the same naming scheme
#
# =============================================================================

# Helper to compute service suffix (empty for prod, -dev/-staging otherwise)
# Usage: suffix=$(just _suffix dev) -> "-dev"
# Usage: suffix=$(just _suffix prod) -> ""
[private]
[no-cd]
_suffix stage:
    @if [ "{{stage}}" = "prod" ]; then echo ""; else echo "-{{stage}}"; fi

# Helper to compute full web service name
[private]
[no-cd]
_web-service stage:
    @if [ "{{stage}}" = "prod" ]; then echo "validibot-web"; else echo "validibot-web-{{stage}}"; fi

# Helper to compute full worker service name
[private]
[no-cd]
_worker-service stage:
    @if [ "{{stage}}" = "prod" ]; then echo "validibot-worker"; else echo "validibot-worker-{{stage}}"; fi

# Helper to compute database instance name
[private]
[no-cd]
_db-instance stage:
    @if [ "{{stage}}" = "prod" ]; then echo "validibot-db"; else echo "validibot-db-{{stage}}"; fi

# Helper to compute secret name
[private]
[no-cd]
_secret-name stage:
    @if [ "{{stage}}" = "prod" ]; then echo "django-env"; else echo "django-env-{{stage}}"; fi

# Helper to compute media bucket name
[private]
[no-cd]
_media-bucket stage:
    @if [ "{{stage}}" = "prod" ]; then echo "validibot-media"; else echo "validibot-media-{{stage}}"; fi

# Helper to compute files bucket name
[private]
[no-cd]
_files-bucket stage:
    @if [ "{{stage}}" = "prod" ]; then echo "validibot-files"; else echo "validibot-files-{{stage}}"; fi

# =============================================================================
# Environment File Paths
# =============================================================================
#
# Each stage has its own environment file with secrets and configuration:
#   - .envs/.production/.django  -> prod stage
#   - .envs/.dev/.django         -> dev stage
#   - .envs/.staging/.django     -> staging stage
#
# These files contain sensitive data and should never be committed to git.
# They are uploaded to each platform's secret management system:
#   - GCP: Secret Manager
#   - AWS: Secrets Manager or Parameter Store
#   - Self-hosted: Docker secrets or mounted volumes
#
# =============================================================================

# Helper to get environment file path for a stage
[private]
[no-cd]
_env-file stage:
    @if [ "{{stage}}" = "prod" ]; then echo ".envs/.production/.django"; else echo ".envs/.{{stage}}/.django"; fi

# =============================================================================
# Display Helpers
# =============================================================================

# Show the current git SHA that would be used for tagging
show-sha:
    @echo "Current git SHA: {{git_sha}}"
    @echo "Images will be tagged with: {{git_sha}}"

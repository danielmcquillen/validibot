# =============================================================================
# Validibot Cloud — Local Development
# =============================================================================
#
# Run the cloud version locally via Docker Compose.
#
# Usage:
#   just local-cloud up            # Start cloud containers (detached)
#   just local-cloud up --build    # Rebuild and start
#
# To stop:
#   just down                # Works for both local and cloud
#
# =============================================================================

cloud_compose := "../validibot-cloud/docker-compose.cloud.yml"

# Start cloud containers locally
[no-cd]
up *args:
    docker compose -f {{cloud_compose}} up -d {{args}}

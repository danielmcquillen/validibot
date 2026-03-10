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

# Rebuild and start containers
#
# Usage: just local-cloud rebuild              # cached rebuild (fast)
#        just local-cloud rebuild --no-cache   # clean rebuild (slow, use after dep upgrades)
#        just local-cloud rebuild worker       # rebuild specific service
[no-cd]
rebuild *args:
    docker compose -f {{cloud_compose}} build {{args}}
    docker compose -f {{cloud_compose}} up -d

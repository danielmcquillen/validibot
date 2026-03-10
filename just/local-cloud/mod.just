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

# Rebuild and start containers, clearing the venv volume
#
# The venv lives in a named Docker volume so it persists across image
# rebuilds.  This recipe removes the volume first, guaranteeing that
# `uv sync` repopulates it from the fresh image (critical after
# dependency upgrades like validibot-shared).
#
# Usage: just local-cloud rebuild              # cached rebuild + fresh venv
#        just local-cloud rebuild --no-cache   # full clean rebuild (slow)
#        just local-cloud rebuild worker       # rebuild specific service
[no-cd]
rebuild *args:
    docker compose -f {{cloud_compose}} down
    -docker volume rm validibot_validibot_local_venv
    docker compose -f {{cloud_compose}} build {{args}}
    docker compose -f {{cloud_compose}} up -d

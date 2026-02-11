# =============================================================================
# Validibot Justfile
# =============================================================================
#
# Just is a modern command runner (like Make, but better).
# Install: brew install just
# Docs: https://just.systems/man/en/
#
# ARCHITECTURE
# ============
#
# This project uses a modular justfile structure to support multiple deployment
# platforms. Commands are organized as follows:
#
#   justfile              <- You are here (root orchestrator)
#   just/
#   ├── common.just       <- Shared variables and helpers
#   ├── local.just        <- Local Docker development
#   ├── gcp/
#   │   └── mod.just      <- Google Cloud Platform deployment
#   ├── aws/
#   │   └── mod.just      <- AWS deployment (stub)
#   └── docker-compose/
#       └── mod.just      <- Docker Compose deployment
#
# USAGE
# =====
#
# Local development (commands imported directly, no prefix):
#   just up               # Start local containers
#   just down             # Stop local containers
#   just logs             # View logs
#   just test             # Run tests
#
# Platform-specific deployment (prefixed with platform name):
#   just gcp deploy prod          # Deploy to GCP production
#   just gcp logs dev             # View GCP dev logs
#   just aws deploy prod          # Deploy to AWS (not yet implemented)
#   just docker-compose deploy    # Deploy Docker Compose production
#
# Platform modules use namespaced commands to avoid conflicts and make it
# clear which platform you're operating on.
#
# TIPS
# ====
#   - Tab completion: Add to ~/.zshrc: eval "$(just --completions zsh)"
#   - Run from subdirectory: just will find this justfile automatically
#   - See what a command does: just --show <command>
#   - Dry run: just --dry-run <command>
#   - List all commands: just --list
#   - List module commands: just gcp --list
#
# ADDING A NEW PLATFORM
# =====================
#
# To add support for a new cloud platform:
#
#   1. Create just/<platform>/mod.just
#   2. Add: mod <platform> 'just/<platform>'  (below)
#   3. Follow the structure of just/gcp/mod.just
#   4. Aim for command parity where it makes sense
#
# See just/aws/mod.just for a template with implementation notes.
#
# =============================================================================

# =============================================================================
# Settings
# =============================================================================

# Load .env file if present (optional, for local dev)
set dotenv-load := false

# Use bash for shell commands (more predictable than sh)
set shell := ["bash", "-cu"]

# =============================================================================
# Imports
# =============================================================================
#
# Imported files merge their recipes into the root namespace.
# These are used for commands you want to run without a prefix.
#
# Prefix with ? to make optional (won't error if file doesn't exist).
# =============================================================================

# Shared configuration and helper functions
import 'just/common.just'

# Local Docker development commands (up, down, build, logs, etc.)
import 'just/local.just'

# =============================================================================
# Modules
# =============================================================================
#
# Modules create namespaced command groups, invoked with: just <module> <command>
# This keeps platform-specific commands organized and avoids conflicts.
#
# Use: mod <name> '<path>'
# Access: just <name> <recipe>
#
# =============================================================================

# Google Cloud Platform deployment
# Usage: just gcp <command>
# Examples:
#   just gcp deploy prod
#   just gcp logs dev
#   just gcp status-all
mod gcp 'just/gcp'

# Amazon Web Services deployment (stub - not yet implemented)
# Usage: just aws <command>
# Status: Commands show "not implemented" message with implementation guidance
mod aws 'just/aws'

# Docker Compose production deployment
# Usage: just docker-compose <command>
# Examples:
#   just docker-compose deploy
#   just docker-compose backup-db
#   just docker-compose health-check
mod docker-compose 'just/docker-compose'

# =============================================================================
# Default Command
# =============================================================================

# List all available commands (this is the default when you just run 'just')
default:
    @echo ""
    @echo "Validibot Command Runner"
    @echo "========================"
    @echo ""
    @echo "Local Development:"
    @just --list --unsorted 2>/dev/null | grep -E "^    (up|down|build|logs|ps|restart|clean|shell|migrate|test|manage)" || true
    @echo ""
    @echo "Platform Modules:"
    @echo "    just gcp <command>        # Google Cloud Platform"
    @echo "    just aws <command>        # AWS (not implemented)"
    @echo "    just docker-compose <command> # Docker Compose production"
    @echo ""
    @echo "Examples:"
    @echo "    just up                   # Start local dev containers"
    @echo "    just gcp deploy prod      # Deploy to GCP production"
    @echo "    just gcp --list           # List all GCP commands"
    @echo "    just docker-compose deploy # Deploy Docker Compose production"
    @echo ""
    @echo "Run 'just --list' for full command list"
    @echo "Run 'just <module> --list' for module commands (e.g., just gcp --list)"
    @echo ""

# =============================================================================
# Cross-Platform Commands
# =============================================================================
#
# These recipes work with any platform by taking a platform argument.
# They're convenience wrappers that delegate to the appropriate module.
#
# Note: For most operations, prefer using the module directly:
#   just gcp deploy prod    (instead of: just deploy gcp prod)
#
# =============================================================================

# Show deployment status for a platform and stage
# Usage: just status gcp prod | just status docker-compose
[no-cd]
platform-status platform stage="":
    #!/usr/bin/env bash
    case "{{platform}}" in
        gcp)
            if [ -z "{{stage}}" ]; then
                just gcp status-all
            else
                just gcp status {{stage}}
            fi
            ;;
        docker-compose)
            just docker-compose status
            ;;
        aws)
            just aws status {{stage}}
            ;;
        *)
            echo "Unknown platform: {{platform}}"
            echo "Supported: gcp, aws, docker-compose"
            exit 1
            ;;
    esac

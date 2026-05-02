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
#   └── self-hosted/
#       └── mod.just      <- Self-hosted deployment (Docker Compose on a VM)
#
# USAGE
# =====
#
# Local Docker development (commands namespaced by flavour):
#   just local up              # Community-only stack
#   just local-pro up          # Community + validibot-pro
#   just local-cloud up        # Community + validibot-pro + validibot-cloud
#   just local down            # Stop containers (same pattern for each flavour)
#   just local logs            # View logs
#   just local test            # Run tests
#
# Platform-specific deployment (prefixed with platform name):
#   just gcp deploy prod          # Deploy to GCP production
#   just gcp logs dev             # View GCP dev logs
#   just aws deploy prod          # Deploy to AWS (not yet implemented)
#   just self-hosted deploy       # Deploy a self-hosted instance (single VM)
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

# Local Docker development — community-only stack (no commercial add-ons).
# Usage: just local <command>
# Examples:
#   just local up
#   just local up --build
#   just local down
#   just local logs
mod local 'just/local'

# Google Cloud Platform deployment
# Usage: just gcp <command>
# Examples:
#   just gcp deploy prod
#   just gcp logs dev
#   just gcp status-all
mod gcp 'just/gcp'

# MCP server — standalone FastMCP image operations (build, deploy,
# secrets, logs, tests).
#
# Historical entry point. Prefer ``just gcp mcp <command>`` for GCP
# work — that grammar is symmetric with ``just gcp django <command>``
# and scopes MCP operations under their deploy target. Both paths
# reach the same module; neither deprecates the other.
#
# The test recipes (``just mcp test``, ``just mcp test-e2e``) are
# genuinely target-agnostic and stay naturally accessed via this
# top-level mount.
#
# Usage:
#   just mcp test                        # local pytest + ruff on mcp/
#   just mcp deploy prod                 # same as ``just gcp mcp deploy prod``
mod mcp 'just/mcp'

# Amazon Web Services deployment (stub - not yet implemented)
# Usage: just aws <command>
# Status: Commands show "not implemented" message with implementation guidance
mod aws 'just/aws'

# Self-hosted deployment (Docker Compose on a single VM)
#
# This is the customer-operated target — the same substrate as
# ``just local`` but deployed to a customer's VM (DigitalOcean, AWS EC2,
# Hetzner, on-prem, etc.) for production use. Self-hosted is single-stage
# per VM (one VM = one stage); recipes do not take a stage argument.
#
# Usage: just self-hosted <command>
# Examples:
#   just self-hosted deploy
#   just self-hosted doctor
#   just self-hosted backup-db
#   just self-hosted health-check
mod self-hosted 'just/self-hosted'

# Pro version local development (community + validibot-pro, no cloud layer)
# Usage: just local-pro up
# Usage: just local-pro up --build
# Usage: ENABLE_MCP_SERVER=true just local-pro up   # include MCP container
mod local-pro 'just/local-pro'

# Cloud version local development (layers validibot-cloud on local stack)
# Usage: just local-cloud up
# Usage: just local-cloud up --build
mod local-cloud 'just/local-cloud'

# =============================================================================
# Default Command
# =============================================================================

# List all available commands (this is the default when you just run 'just')
default:
    @echo ""
    @echo "Validibot Command Runner"
    @echo "========================"
    @echo ""
    @echo "Local Docker (pick the flavour you need):"
    @echo "    just local <command>        # Community only"
    @echo "    just local-pro <command>    # Community + validibot-pro"
    @echo "    just local-cloud <command>  # Community + pro + cloud"
    @echo ""
    @echo "Each local flavour supports: up, up --build, down, rebuild, logs, ..."
    @echo ""
    @echo "Platform Modules:"
    @echo "    just gcp <command>             # Google Cloud Platform"
    @echo "    just gcp django <command>      # Django-only GCP ops (e.g. secrets)"
    @echo "    just gcp mcp <command>         # MCP-only GCP ops (secrets, deploy, ...)"
    @echo "    just mcp <command>             # MCP operations (alias; also: local tests)"
    @echo "    just aws <command>             # AWS (not implemented)"
    @echo "    just self-hosted <command>     # Self-hosted (Docker Compose on a VM)"
    @echo ""
    @echo "Examples:"
    @echo "    just local up             # Start community dev stack"
    @echo "    just local-pro up         # Start community + pro"
    @echo "    just gcp deploy prod      # Deploy to GCP production"
    @echo "    just gcp --list           # List all GCP commands"
    @echo ""
    @echo "Run 'just --list' for full command list"
    @echo "Run 'just <module> --list' for module commands (e.g., just local --list)"
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
# Usage: just status gcp prod | just status self-hosted
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
        self-hosted)
            just self-hosted status
            ;;
        aws)
            just aws status {{stage}}
            ;;
        *)
            echo "Unknown platform: {{platform}}"
            echo "Supported: gcp, aws, self-hosted"
            exit 1
            ;;
    esac

# =============================================================================
# Release
# =============================================================================
#
# Cuts a signed-tag release for the validibot Django app. CI then
# verifies the signature, generates a CycloneDX SBOM, and creates a
# GitHub release with the SBOM attached.
#
# Operator verification (after clone): see RELEASING.md.

# Release a new version: signs the tag, pushes, CI publishes the GitHub release.
# Usage: just release 0.4.0
release VERSION:
    #!/usr/bin/env bash
    set -euo pipefail

    # Validate version format.
    if [[ ! "{{VERSION}}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "✗ Version must be in format X.Y.Z (e.g., 0.4.0). Got: {{VERSION}}"
        exit 1
    fi

    # Refuse if working tree is dirty.
    if [[ -n $(git status --porcelain) ]]; then
        echo "✗ Working tree has uncommitted changes. Commit or stash first."
        git status --short
        exit 1
    fi

    # Refuse if not on main.
    BRANCH=$(git branch --show-current)
    if [[ "$BRANCH" != "main" ]]; then
        echo "✗ Not on main branch (currently on '$BRANCH')."
        echo "  Releases are cut from main only. Switch with: git checkout main"
        exit 1
    fi

    # Refuse if tag already exists locally or remotely.
    TAG="v{{VERSION}}"
    if git rev-parse "$TAG" >/dev/null 2>&1; then
        echo "✗ Tag $TAG already exists locally."
        exit 1
    fi
    if git ls-remote --tags origin "refs/tags/$TAG" | grep -q "$TAG"; then
        echo "✗ Tag $TAG already exists on origin."
        exit 1
    fi

    # Confirm we're up-to-date with origin.
    git fetch origin main
    if [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
        echo "✗ Local main is not in sync with origin/main."
        echo "  Run: git pull"
        exit 1
    fi

    # Verify the cross-repo dependency on validibot-shared is at the
    # latest published version. Catches the "I forgot to bump
    # validibot-shared after publishing a new shared release"
    # failure mode — easy to miss, hard to debug after the release
    # is out (deployed code uses an older shared schema).
    #
    # Override with VALIDIBOT_RELEASE_ALLOW_STALE_SHARED=1 for
    # emergencies (e.g. PyPI is down, or you intentionally want to
    # pin to an older shared release).
    if [[ "${VALIDIBOT_RELEASE_ALLOW_STALE_SHARED:-0}" != "1" ]]; then
        SHARED_PINNED=$(grep -E '"validibot-shared==' pyproject.toml | head -1 | sed -E 's/.*"validibot-shared==([^"]+)".*/\1/')
        if [[ -z "$SHARED_PINNED" ]]; then
            echo "⚠ Could not detect validibot-shared pin in pyproject.toml; skipping freshness check."
        else
            SHARED_LATEST=$(curl -s --max-time 10 https://pypi.org/pypi/validibot-shared/json 2>/dev/null | jq -r '.info.version' 2>/dev/null)
            if [[ -z "$SHARED_LATEST" ]] || [[ "$SHARED_LATEST" == "null" ]]; then
                echo "⚠ Could not query PyPI for latest validibot-shared. Currently pinned: $SHARED_PINNED."
                echo "  Press Enter to continue anyway, Ctrl+C to abort..."
                read -r
            elif [[ "$SHARED_PINNED" != "$SHARED_LATEST" ]]; then
                echo "✗ validibot-shared is pinned to $SHARED_PINNED but latest on PyPI is $SHARED_LATEST."
                echo ""
                echo "  Update pyproject.toml so the line reads:"
                echo "      \"validibot-shared==$SHARED_LATEST\","
                echo ""
                echo "  Then commit + push, and re-run: just release {{VERSION}}"
                echo ""
                echo "  Override (emergencies only): VALIDIBOT_RELEASE_ALLOW_STALE_SHARED=1 just release {{VERSION}}"
                exit 1
            else
                echo "✓ validibot-shared is at latest ($SHARED_LATEST)"
            fi
        fi
    fi

    echo ""
    echo "About to sign and push tag $TAG."
    echo "Press Enter to continue, Ctrl+C to abort..."
    read -r

    # Sign the tag. Requires `git config --global tag.gpgsign true`
    # and a signing key configured. The CI workflow at
    # .github/workflows/release.yml verifies the signature and
    # publishes the GitHub release with the SBOM.
    git tag -s "$TAG" -m "$TAG"
    git push origin "$TAG"

    echo ""
    echo "✓ Pushed $TAG"
    echo "  CI will verify the signature and publish the release."
    echo "  Monitor: gh run watch"

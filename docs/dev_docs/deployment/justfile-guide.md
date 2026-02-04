# Just Command Runner Guide

Validibot uses [Just](https://just.systems/man/en/) as its command runner for managing local development and deployments across multiple platforms. Just is similar to Make but with a cleaner syntax, better error messages, and modern features like modules and imports.

## Why Just?

We chose Just over alternatives (Make, shell scripts, Python scripts) because:

- **Simplicity**: Clean, readable syntax without Make's arcane rules
- **Modules**: Native support for organizing commands by platform/concern
- **Cross-platform**: Works on macOS, Linux, and Windows
- **No dependencies**: Single binary with no runtime requirements
- **Tab completion**: Built-in shell completion for discoverability
- **Dry-run mode**: Preview commands before executing them

## Installation

```bash
# macOS
brew install just

# Linux (via Homebrew)
brew install just

# Linux (via cargo)
cargo install just

# Enable tab completion (add to ~/.zshrc or ~/.bashrc)
eval "$(just --completions zsh)"  # or bash/fish
```

## File Structure

Our justfile setup uses a modular architecture to support multiple deployment platforms:

```
justfile              # Root orchestrator
just/
├── common.just       # Shared variables and helpers
├── local.just        # Local Docker development commands
├── gcp/
│   └── mod.just      # Google Cloud Platform deployment
├── aws/
│   └── mod.just      # AWS deployment (stub - not implemented)
└── selfhosted/
    └── mod.just      # Self-hosted Docker Compose deployment
```

### Root Justfile

The root `justfile` serves as the orchestrator. It:

- Sets global configuration (shell, dotenv settings)
- **Imports** shared and local development recipes (merged into root namespace)
- Declares **modules** for each deployment platform (namespaced)

### Imports vs Modules

We use two mechanisms for organizing recipes:

| Feature | Imports | Modules |
|---------|---------|---------|
| Syntax | `import 'path.just'` | `mod name 'path'` |
| Access | Direct: `just recipe` | Namespaced: `just module recipe` |
| Use case | Frequently used commands | Platform-specific commands |

**Imports** (local development) are accessed directly because you use them constantly:
```bash
just up          # Start containers
just logs        # View logs
just test        # Run tests
```

**Modules** (deployment platforms) are namespaced to make the target platform explicit:
```bash
just gcp deploy prod        # Deploy to GCP
just selfhosted deploy      # Deploy self-hosted
```

## Quick Reference

### Local Development

These commands are imported directly (no prefix needed):

```bash
# Container lifecycle
just up              # Start all local containers
just down            # Stop containers
just build           # Rebuild and restart
just logs            # Follow all container logs
just ps              # Show container status
just restart         # Stop then start
just clean           # Stop and remove volumes (deletes data!)

# Django commands
just shell           # Bash shell in Django container
just migrate         # Run database migrations
just manage "cmd"    # Run any manage.py command

# Testing
just test            # Run tests locally
just test -k "name"  # Run specific tests
just test-integration # Run integration tests with Docker
```

### Google Cloud Platform

Commands are prefixed with `gcp`:

```bash
# List all GCP commands
just --list gcp

# Deployment
just gcp deploy prod         # Deploy web service
just gcp deploy-worker prod  # Deploy worker service
just gcp deploy-all prod     # Deploy both services

# Operations
just gcp logs prod           # View logs
just gcp logs-follow prod    # Stream logs
just gcp status prod         # Check service status
just gcp status-all          # Status of all stages
just gcp open prod           # Open in browser

# Database & Django
just gcp migrate prod                  # Run migrations
just gcp setup-data prod               # Initialize data
just gcp management-cmd prod "shell"   # Run any command

# Secrets
just gcp secrets prod                  # Upload secrets
just gcp secrets-init dev              # Create env template

# Infrastructure
just gcp init-stage dev                # Create all infrastructure
just gcp kms-setup dev                 # Set up KMS signing key
just gcp scheduler-setup dev           # Create scheduled jobs
just gcp lb-setup prod "example.com"   # Set up load balancer

# Maintenance
just gcp maintenance-on dev            # Put in maintenance mode
just gcp maintenance-off dev           # Resume from maintenance
```

### Self-Hosted Docker Compose

Commands are prefixed with `selfhosted`:

```bash
# List all selfhosted commands
just --list selfhosted

# Deployment
just selfhosted deploy       # Build and start all services
just selfhosted update       # Full update with backup

# Operations
just selfhosted status       # Check container status
just selfhosted logs         # Follow logs
just selfhosted health-check # Verify services are running

# Database
just selfhosted migrate              # Run migrations
just selfhosted backup-db            # Create database backup
just selfhosted restore-db file.gz   # Restore from backup

# Maintenance
just selfhosted maintenance-on       # Enter maintenance mode
just selfhosted maintenance-off      # Exit maintenance mode
```

### AWS (Stub)

AWS support is planned but not yet implemented. Commands show helpful guidance:

```bash
just aws deploy prod    # Shows implementation guidance
```

## Stages

All platforms support three deployment stages:

| Stage | Purpose | Resource Naming |
|-------|---------|-----------------|
| `dev` | Development and testing | `*-dev` suffix |
| `staging` | Pre-production testing | `*-staging` suffix |
| `prod` | Production | No suffix |

Example resource names:

- Dev: `validibot-web-dev`, `validibot-db-dev`
- Prod: `validibot-web`, `validibot-db`

## Common Patterns

### Deploying Updates

```bash
# GCP: Deploy to dev first, then prod
just gcp deploy dev
just gcp migrate dev
just gcp verify-deployment-quick dev

# After testing on dev
just gcp deploy prod
just gcp migrate prod

# Self-hosted: Single command update
just selfhosted update
```

### Viewing Logs

```bash
# GCP: Last 50 log entries
just gcp logs prod

# GCP: Stream logs in real-time
just gcp logs-follow prod

# Self-hosted: Follow container logs
just selfhosted logs
```

### Running Django Commands

```bash
# Local development
just manage "createsuperuser"
just manage "shell_plus"

# GCP (creates temporary Cloud Run Job)
just gcp management-cmd prod "createsuperuser"

# Self-hosted
just selfhosted manage "createsuperuser"
```

### Checking Status

```bash
# GCP: All stages at once
just gcp status-all

# GCP: Specific stage
just gcp status dev

# Self-hosted
just selfhosted health-check
```

## Tips & Tricks

### Preview Commands

See what a command will do without executing:

```bash
just --show gcp deploy
just --dry-run gcp deploy prod
```

### List All Commands

```bash
just --list              # Root commands
just --list gcp          # GCP commands
just --list selfhosted   # Self-hosted commands
```

### Run from Anywhere

Just automatically finds the justfile in parent directories:

```bash
cd validibot/core/
just up  # Still works!
```

### Environment Variables

Commands that need environment variables:

```bash
# GCP: Connect to Cloud SQL
DATABASE_PASSWORD='secret' just gcp local-to-gcp-shell dev

# Testing: E2E tests
E2E_TEST_API_URL=https://... just gcp test-e2e
```

## Adding a New Platform

To add support for a new cloud platform:

1. Create `just/<platform>/mod.just`
2. Add `mod <platform> 'just/<platform>'` to root justfile
3. Follow the structure of `just/gcp/mod.just`
4. Implement the core commands: `deploy`, `migrate`, `logs`, `status`

See `just/aws/mod.just` for a template with detailed implementation notes.

## Migrating from Old Commands

If you have scripts or muscle memory from the old flat justfile:

| Old Command | New Command |
|-------------|-------------|
| `just gcp-deploy dev` | `just gcp deploy dev` |
| `just gcp-logs prod` | `just gcp logs prod` |
| `just gcp-status-all` | `just gcp status-all` |
| `just gcp-migrate dev` | `just gcp migrate dev` |
| `just gcp-secrets prod` | `just gcp secrets prod` |

The new structure uses a space instead of a hyphen after the platform name.

## Troubleshooting

### "Module not found" Error

Ensure you're running Just version 1.31.0 or later:

```bash
just --version
```

### "Recipe not found" Error

Check that you're using the correct prefix:

```bash
# Wrong: deployment commands need platform prefix
just deploy prod

# Correct: use the platform prefix
just gcp deploy prod
```

### Tab Completion Not Working

Ensure you've added the completion to your shell profile:

```bash
# Add to ~/.zshrc
eval "$(just --completions zsh)"

# Then reload
source ~/.zshrc
```

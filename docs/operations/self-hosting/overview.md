# Self-Hosted Validibot — Overview

This is the entry point for operators running Validibot on their own
infrastructure. If you're a Validibot developer rather than an
operator, you probably want `docs/dev_docs/` instead.

## What "self-hosted" means

A self-hosted deployment is Validibot running on a single Linux VM
that **you** control — typically on DigitalOcean, AWS EC2, Hetzner,
or your own on-prem hardware. You own the host, the data, the
backups, the upgrade cadence, and the support escalation path.

Self-hosted is one of three deployment targets Validibot supports:

| Target | Substrate | Audience | Driver |
|---|---|---|---|
| **local** | Docker Compose on your laptop | a single developer | `just local <cmd>` |
| **self-hosted** | Docker Compose on a VM | customers running their own copy | `just self-hosted <cmd>` |
| **GCP** | Cloud Run, Cloud SQL, Cloud Tasks | Validibot's hosted offering | `just gcp <cmd>` |

The same Validibot codebase serves all three. The differences are in
the substrate (where containers actually run) and the operational
expectations (developer testing vs production data custody vs cloud
SaaS).

## What's on the VM

```text
your VM
├── Docker Engine + Compose plugin
├── /srv/validibot/ (recommended data root)
└── A Validibot Compose stack:
    ├── web        Django web application (Gunicorn)
    ├── worker     Celery worker (background tasks, validators)
    ├── scheduler  Celery Beat (periodic tasks)
    ├── postgres   Database
    ├── redis      Task queue broker
    ├── caddy      (optional, opt-in) reverse proxy with auto-TLS
    └── mcp        (Pro feature, opt-in) FastMCP server
```

Validation jobs spawn additional short-lived Docker containers
("advanced validators" — EnergyPlus, FMU, etc.) via the worker's
access to the host Docker socket. These are run-scoped and clean up
after themselves.

## Recommended VM sizing

| Use case | CPU | Memory | Disk | Notes |
|---|---:|---:|---:|---|
| Evaluation | 2 vCPU | 4 GB | 50 GB | Small demo workflows |
| Small team | 4 vCPU | 8-16 GB | 200 GB SSD | EnergyPlus/FMU light use |
| Heavy simulation | 8+ vCPU | 32 GB | 500 GB+ SSD | More validator concurrency |

For paid pilots we recommend the "small team" size with a separate
block-storage volume mounted at `/srv/validibot` so data and evidence
aren't on a disposable boot disk. See the DigitalOcean tutorial under
`providers/` for one provider's specifics.

## What outbound calls happen by default

Self-hosted Validibot is **telemetry-off by default**. No product
analytics. No license phone-home. No usage reporting. The default
install makes only these outbound calls:

- container image pulls during install/upgrade (Docker Hub / GHCR);
- email delivery if you configure an email provider;
- Let's Encrypt ACME challenges if you enable the bundled Caddy proxy;
- nothing else.

Sentry error reporting and other diagnostics can be opted in per
section 8 of `.envs/.production/.self-hosted/.django`. None of them
are required for the application to validate, sign, back up, restore,
or export evidence.

## Two editions: community and Pro

| | Community | Pro |
|---|---|---|
| Cost | Free (AGPL) | Paid license |
| Validators | Built-in (JSON, XML, basic, AI) | + advanced (EnergyPlus, FMU, custom) |
| Workflows | Yes | + teams, guests, signed credentials |
| MCP server | No | Yes |
| Install path | Public Docker images | Build locally from a private wheel |
| Support | Community | Email support included |

**Activating Pro is a settings module switch + a package install.**
Buy a license at https://validibot.com, receive a private package
URL and a Pro wheel reference. Set those in your `.build` env file,
flip `DJANGO_SETTINGS_MODULE` to `config.settings.production_pro` in
`.django`, and run `just self-hosted deploy`. Existing data, users,
workflows, and runs are preserved across the upgrade. Pro migrations
are additive — they add tables but never reshape community tables.

There is no runtime license check inside the application. A running
Pro instance keeps running even if outbound internet is fully cut
off. License expiry blocks future package downloads (your next
upgrade fails); it does not affect the running install.

## First install (one-page summary)

This is the minimum sequence to get Validibot running on a VM. The
provider-specific tutorials under `providers/` go through the same
sequence with provider-specific steps (DNS, firewall, volumes).

```bash
# On a fresh Ubuntu LTS VM with you SSH'd in (as root):

# 1. Clone the repo (or extract a release tarball)
git clone https://github.com/validibot/validibot.git
cd validibot

# 2. Bootstrap the host (installs Docker + Compose + just, creates
#    the validibot user, sets up data dirs). This is the ONLY
#    script you run — everything after this is a just recipe.
#    Phase 0: stub. Implementation lands in a later phase.
./deploy/self-hosted/scripts/bootstrap-host

# Switch to the validibot user — root has nothing more to do.
exit
ssh validibot@<your-vm>
cd /srv/validibot/repo  # or wherever bootstrap-host put the clone

# 3. Copy and edit env files
cp .envs.example/.production/.self-hosted/.django \
   .envs/.production/.self-hosted/.django
cp .envs.example/.production/.self-hosted/.postgres \
   .envs/.production/.self-hosted/.postgres
cp .envs.example/.production/.self-hosted/.build \
   .envs/.production/.self-hosted/.build
$EDITOR .envs/.production/.self-hosted/.django  # set SITE_URL, secrets, etc.

# 4. Validate config and DNS (in just, since just exists now)
just self-hosted check-env
just self-hosted check-dns        # verify SITE_URL resolves to this VM

# 5. Bring up the stack
just self-hosted bootstrap   # builds, migrates, creates superuser, registers OIDC clients

# 6. Verify
just self-hosted health-check

# 7. (Phase 1) Run the doctor diagnostic
just self-hosted doctor

# 8. (Phase 2) Run the end-to-end smoke test
just self-hosted smoke-test
```

Step 7 and 8 print a "not yet implemented" message in Phase 0; the
recipes exist so the operator surface is visible, and they will start
doing real work in later phases. Step 4's `check-dns` is fully
implemented today.

## Day-to-day operations

```bash
just self-hosted --list        # see all recipes
just self-hosted status        # are services running?
just self-hosted logs          # follow logs from all services
just self-hosted health-check  # quick service health
just self-hosted backup-db     # database-only backup (Phase 3 will add full app backup)
just self-hosted update        # pull, rebuild, migrate, restart
```

The fuller operator interface (`doctor`, `smoke-test`, `backup`,
`restore`, `upgrade`, `collect-support-bundle`) lands in later phases.
The names exist today; the implementations are phased.

## Reverse proxy: bring your own, or use bundled Caddy

The Compose stack does not run a reverse proxy by default. Most
operators already have one (nginx, Traefik, Cloudflare Tunnel, hosting
provider load balancer) and would resent another being installed.

If you don't have one, the kit ships an opt-in Caddy service with
automatic Let's Encrypt TLS:

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

Confirm DNS first:

```bash
just self-hosted check-dns
```

If you bring your own proxy, configure it to forward to the `web`
container on port 8000 and set `DJANGO_SECURE_PROXY_SSL_HEADER` plus
`DJANGO_CSRF_TRUSTED_ORIGINS` appropriately in your `.django` file.

## Provider quickstarts

The canonical deployment is "single Linux VM with Docker Compose."
Provider-specific tutorials map that generic shape to real
infrastructure:

- [DigitalOcean](providers/digitalocean.md) — primary. The first
  supported provider quickstart. Outline today; full ten-step tutorial
  in Phase 1.
- AWS EC2, Hetzner, on-prem — substrate-generic install instructions
  apply. Provider tutorials may follow on customer demand. (No
  comparable self-hosted product writes per-provider reference docs;
  Validibot follows the same pattern.)

## Support and ADR reference

For paid Pro support, generate a redacted support bundle and email it
to support@validibot.com:

```bash
# Phase 6 — implementation lands later. The recipe exists today.
just self-hosted collect-support-bundle
```

The bundle includes versions, doctor output, recent logs, migration
state, and validator manifests. It excludes secrets, signing keys,
API tokens, and raw submission contents.

## What's not yet implemented

This page documents the **target shape**. The full deployment kit
lands across ADR phases 0-6:

| Phase | Status | Scope |
|---|---|---|
| Phase 0 | ✓ Done | Naming/terminology rename, kit skeleton, stub recipes, this overview |
| Phase 1 | Sessions 1-2 done | `doctor` command, JSON output, provider overlays, compatibility matrix |
| Phase 2 | Planned | `smoke-test` command, demo workflow |
| Phase 3 | Planned | `backup` and `restore` commands, manifest schema |
| Phase 4 | Planned | `upgrade` workflow with versioned images |
| Phase 5 | Planned | Validator operations (`validators list-images`, etc.), `cleanup` |
| Phase 6 | Planned | Support bundle and pilot kit |

Run `just self-hosted --list` to see which recipes exist today (some
work, some print a Phase 0 stub message — that's expected).

## Detailed guides

The fuller operator documentation:

- **[Install](install.md)** — substrate-generic install steps that work on any Linux + Docker host.
- **[Configuration](configuration.md)** — env file reference, the eight grouped sections of `.django`, settings module switching, deployment profiles.
- **[Backups](backups.md)** — backup architecture, manifest schema, off-host recommendations.
- **[Restore](restore.md)** — restore drills, component selection, recovery patterns.
- **[Upgrades](upgrades.md)** — upgrade lifecycle, pre-flight checks, strict upgrade-path enforcement, in-flight run handling.
- **[Validator Images](validator-images.md)** — what's installed, run-scoped isolation, image pinning, cleanup, future trust tiers.
- **[Security Hardening](security-hardening.md)** — the recommended hardening checklist.
- **[Support Bundle](support-bundle.md)** — what's in the redacted bundle, what's excluded, support workflow contract.
- **[Troubleshooting](troubleshooting.md)** — common issues and how to diagnose them.
- **[Release Notes Policy](release-notes-policy.md)** — what every release announces.
- **[Operator Recipes](operator-recipes.md)** — full reference for `just self-hosted` recipes.
- **[Doctor Check IDs](doctor-check-ids.md)** — every check ID and its fix.

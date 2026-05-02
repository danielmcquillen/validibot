# Installing Validibot (self-hosted)

This is the substrate-generic install guide. It works on any Linux host with Docker. Provider-specific tutorials (DigitalOcean, AWS EC2, Hetzner, on-prem) sit on top of this — see `providers/`.

If you're evaluating Validibot on your laptop, [Run Validibot Locally](../../dev_docs/deployment/deploy-local.md) is faster.

## Prerequisites

A Linux VM with:

- Ubuntu LTS, Debian stable, or another Docker-friendly distribution;
- Docker Engine + Compose plugin (Docker 24+ recommended; Docker 27+ if available);
- 4+ vCPU, 8+ GB RAM, 50+ GB disk for evaluation; more for production (see sizing guide in `overview.md`);
- a public IP and DNS record if you want HTTPS;
- root or sudo access for the initial bootstrap.

The `bootstrap-host` helper installs Docker, creates the `validibot` user, and sets up data directories. If you already have Docker installed and a non-root user with Docker access, you can skip `bootstrap-host` and run the `just self-hosted bootstrap` step directly.

## Recommended VM sizing

| Use case | CPU | Memory | Disk | Notes |
|---|---:|---:|---:|---|
| Evaluation | 2 vCPU | 4 GB | 50 GB | Small demo workflows |
| Small team | 4 vCPU | 8-16 GB | 200 GB SSD | EnergyPlus/FMU light use |
| Heavy simulation | 8+ vCPU | 32 GB | 500 GB+ SSD | More worker/validator concurrency |

For paid pilots we recommend the "small team" size with a separate block-storage volume mounted at `/srv/validibot` so data and evidence aren't on a disposable boot disk.

## Install steps

### 1. Clone the repo (or extract a release tarball)

```bash
git clone https://github.com/validibot/validibot.git
cd validibot
```

For air-gapped or release-tarball installs, extract the tarball and `cd` into the resulting directory. The tarball bundles `docker-compose.production.yml`, the `deploy/self-hosted/` directory, the `just/self-hosted/` module, and a copy of the env templates.

### 2. Bootstrap the host (as root)

```bash
./deploy/self-hosted/scripts/bootstrap-host
```

What it does:

- installs Docker Engine + Compose plugin;
- installs `just` (a small Rust binary);
- creates a `validibot` system user with Docker group membership;
- creates `/srv/validibot/` (or your `--data-root`) and sets ownership;
- creates the data, runs, and evidence subdirectories with the right modes.

This is the **only** script you run as root. Everything after this is a `just` recipe run as the `validibot` user.

After bootstrap, switch users:

```bash
exit  # leave root
ssh validibot@<your-vm>
cd /srv/validibot/repo  # or wherever bootstrap-host put the clone
```

### 3. Copy and edit env files

```bash
cp -r .envs.example/.production/.self-hosted/ .envs/.production/.self-hosted/
$EDITOR .envs/.production/.self-hosted/.django
```

The `.django` file has eight grouped sections you'll need to customise:

1. **Required** — `SITE_URL`, `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS`, `DJANGO_MFA_ENCRYPTION_KEY`;
2. **URLs/security** — `DJANGO_CSRF_TRUSTED_ORIGINS`, secure cookies, HSTS;
3. **Database/cache** — usually defaults;
4. **Storage** — `DATA_STORAGE_ROOT`;
5. **Email** — `DJANGO_EMAIL_BACKEND`, sender addresses;
6. **Validators** — runner selection, image policy;
7. **Pro/signing** — empty for community, set when activating Pro;
8. **Optional telemetry** — off by default.

Generate secrets:

```bash
# DJANGO_SECRET_KEY
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# DJANGO_MFA_ENCRYPTION_KEY (must be Fernet-format)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Also edit `.postgres` and `.build` from the same template.

### 4. Validate config and DNS

```bash
just self-hosted check-env
just self-hosted check-dns       # verify SITE_URL resolves to this VM
```

### 5. Bring up the stack

```bash
just self-hosted bootstrap      # builds, migrates, creates superuser, registers OIDC clients
```

### 6. Verify

```bash
just self-hosted health-check   # quick service health
just self-hosted doctor         # full diagnostic
just self-hosted smoke-test     # end-to-end demo workflow
```

The doctor command is target-aware and emits a stable JSON schema (`validibot.doctor.v1`). Pass `--json` for machine-readable output, `--strict` to fail CI on warnings.

## Day-to-day operations

Once installed:

```bash
just self-hosted --list           # all recipes
just self-hosted status           # are services running?
just self-hosted logs             # follow logs from all services
just self-hosted health-check     # quick service health
just self-hosted doctor           # full diagnostic
just self-hosted backup           # full application backup
just self-hosted upgrade --to v0.9.0   # versioned upgrade
just self-hosted cleanup          # remove stale containers/images/backups
```

See [operator-recipes.md](operator-recipes.md) for the full reference.

## Reverse proxy: bring your own, or use bundled Caddy

The Compose stack does not run a reverse proxy by default. Most operators already have one (nginx, Traefik, Cloudflare Tunnel, hosting provider load balancer) and would resent another being installed.

If you don't have one, the kit ships an opt-in Caddy service with automatic Let's Encrypt TLS:

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

Confirm DNS first:

```bash
just self-hosted check-dns
```

If you bring your own proxy, configure it to forward to the `web` container on port 8000 and set `DJANGO_SECURE_PROXY_SSL_HEADER` plus `DJANGO_CSRF_TRUSTED_ORIGINS` appropriately in your `.django` file.

## Provider quickstarts

The canonical deployment is "single Linux VM with Docker Compose." Provider-specific tutorials map that generic shape to real infrastructure:

- [DigitalOcean](providers/digitalocean.md) — primary supported provider tutorial.
- AWS EC2, Hetzner, on-prem — substrate-generic install instructions apply. Provider tutorials may follow on customer demand.

## Activating Pro

If you've bought a Pro license:

1. Edit `.envs/.production/.self-hosted/.build`:

   ```bash
   VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro==<version>
   VALIDIBOT_PRIVATE_INDEX_URL=https://<email>:<token>@pypi.validibot.com/simple/
   ```

2. Edit `.envs/.production/.self-hosted/.django`:

   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.production_pro
   ```

3. Re-run deploy:

   ```bash
   just self-hosted deploy
   just self-hosted doctor
   ```

That's it. Existing data, users, workflows, and runs are preserved across the upgrade. Pro migrations are additive — they add tables (teams, guests, signed credentials, MCP, advanced analytics) but never reshape community tables. The package-index credential is the entitlement gate; there is no runtime license phone-home.

## What's not yet implemented

This page documents the **target shape**. The full deployment kit lands across ADR phases 0-6:

| Phase | Status | Scope |
|---|---|---|
| Phase 0 | ✓ Done | Naming/terminology rename, kit skeleton, stub recipes, this overview |
| Phase 1 | Sessions 1-2 done | `doctor` command, JSON output, provider overlays, compatibility matrix |
| Phase 2 | Planned | `smoke-test` command, demo workflow |
| Phase 3 | Planned | `backup` and `restore` commands, manifest schema |
| Phase 4 | Planned | `upgrade` workflow with versioned images |
| Phase 5 | Planned | Validator operations (`validators list-images`, etc.), `cleanup` |
| Phase 6 | Planned | Support bundle and pilot kit |

Run `just self-hosted --list` to see which recipes exist today (some work, some print a Phase 0 stub message — that's expected).

## See also

- [Self-Hosting Overview](overview.md)
- [Configuration](configuration.md) — env file reference
- [Backups](backups.md) — backup architecture and policy
- [Restore](restore.md) — restore drill workflow
- [Upgrades](upgrades.md) — upgrade lifecycle
- [Security Hardening](security-hardening.md) — recommended hardening
- [Support Bundle](support-bundle.md) — what's in it, what's redacted
- [DigitalOcean Provider Guide](providers/digitalocean.md)

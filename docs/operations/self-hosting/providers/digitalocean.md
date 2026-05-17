# Self-Hosting Validibot on DigitalOcean

This is the canonical end-to-end tutorial for getting a production-ready Validibot instance running on a DigitalOcean Droplet. It is the single source of truth for DigitalOcean — every other doc that mentions DO links here rather than repeating instructions.

By the end of this guide you'll have:

- A Validibot instance reachable at your own domain with a valid Let's Encrypt TLS certificate.
- Application data and Postgres files on a separate block-storage volume so they survive Droplet rebuilds.
- A DigitalOcean Cloud Firewall locking inbound traffic down to SSH (your IP only) plus 80/443.
- A documented backup, restore-drill, and upgrade workflow.
- A clear path to activate Pro (signed credentials, teams, guests, MCP, advanced analytics) if and when you license it.

The same instance can run the community edition (free, AGPL) or be upgraded to Pro by changing two env values and re-running `just self-hosted deploy`. Existing data is preserved across the switch.

## Prerequisites

- A DigitalOcean account with billing set up.
- A registered domain you can add an A-record to (e.g. `validibot.example.org`). You don't need to host DNS at DigitalOcean — any registrar works.
- An SSH keypair already uploaded to DigitalOcean. Password auth is disabled throughout this guide.
- A laptop with `git`, `ssh`, and (optionally) `doctl` installed.
- Optional: a Validibot Pro license. Community works without one — see [Step 12](#step-12-optional-activate-pro).

If you'd rather evaluate locally before committing to a Droplet, [Run Validibot Locally](../../../dev_docs/deployment/deploy-local.md) takes about ten minutes.

## Sizing and cost

The right Droplet size depends almost entirely on which validators you'll run. The built-in validators (JSON Schema, XML Schema, Basic, AI) run in the Django process and use little memory. Advanced validators (EnergyPlus, FMU, custom containers) spawn short-lived sibling containers that can each consume 2-4 GB during a simulation.

### Memory budget for the base stack

| Component | Idle | Peak |
|---|---|---|
| Django (Gunicorn, 2 workers) | ~150-200 MB | ~400 MB |
| Celery worker | ~150 MB | ~300 MB |
| Celery beat (scheduler) | ~80 MB | ~100 MB |
| PostgreSQL | ~100 MB | ~300 MB |
| Redis | ~50 MB | ~100 MB |
| Caddy (if bundled) | ~20 MB | ~50 MB |
| OS + Docker overhead | ~300 MB | ~400 MB |
| **Total** | **~850 MB** | **~1.65 GB** |

An EnergyPlus container layered on top of this needs another 2-4 GB. A 2 GB Droplet running the base stack has roughly 350 MB headroom — not enough for even a small EnergyPlus simulation, so it'll OOM-kill other services when one runs.

### Recommended Droplet sizes

| Use case | Droplet | Monthly | Notes |
|---|---|---|---|
| Built-in validators only | 2 GB / 1 vCPU | $12 | JSON, XML, Basic, AI — no EnergyPlus or FMU |
| Occasional advanced validators | 4 GB / 2 vCPU | $24 | Add swap; may queue during heavy use |
| **Paid pilot (recommended)** | **8 GB / 4 vCPU** | **$48** | **Regular advanced validator usage** |
| High-volume production | 16 GB / 8 vCPU | $96 | Multiple concurrent advanced validations |

For paid pilots we strongly recommend the 8 GB tier plus a **200 GB block-storage volume mounted at `/srv/validibot`** so application data and Postgres files are not on the disposable boot disk. That combination is what the rest of this guide assumes.

### Total monthly cost

| Configuration | Components | Monthly |
|---|---|---|
| Minimal (built-in validators) | 2 GB Droplet | $12 |
| Small (occasional advanced) | 4 GB Droplet + swap | $24 |
| **Recommended (paid pilot)** | **8 GB Droplet + 200 GB volume** | **$48 + $20** |
| Production (managed DB) | 8 GB Droplet + 200 GB volume + Managed Postgres | $83+ |

Optional add-ons (covered in [Step 13](#step-13-optional-enhancements)): Managed PostgreSQL ($15-30/mo), Spaces ($5/mo for 250 GB + CDN), Load Balancer ($12/mo).

## Architecture

What DigitalOcean owns:

- VM provisioning, networking, block storage, snapshots, optional Managed Postgres, Cloud Firewall.

What you (the operator) own:

- Compose configuration, app secrets, migrations, validation data, evidence bundles, application-level backups, restore tests, upgrades, and the support escalation path.

What's on the Droplet:

```text
Droplet (Ubuntu 24.04 LTS)
├── Docker Engine + Compose plugin
├── /srv/validibot/           (mounted block-storage volume)
│   ├── repo/                 (this repository, cloned)
│   ├── data/                 (DATA_STORAGE_ROOT — submissions, evidence)
│   └── backups/              (application-level backups)
└── A Validibot Compose stack:
    ├── web        Django web application (Gunicorn)
    ├── worker     Celery worker (background tasks, validators)
    ├── scheduler  Celery Beat (periodic tasks)
    ├── postgres   Database
    ├── redis      Task queue broker
    ├── caddy      (opt-in profile) reverse proxy with auto-TLS
    └── mcp        (Pro feature, opt-in) FastMCP server
```

Advanced validators (EnergyPlus, FMU) run as short-lived sibling containers spawned by the worker via the host Docker socket. They clean up after themselves.

---

## Step 1: Create the Droplet

Ubuntu 24.04 LTS x64, in your customer's preferred region, with SSH-key login only.

### Using the DigitalOcean control panel

1. **Create → Droplets**.
2. **Choose an image:** Marketplace → **Docker on Ubuntu 24.04** (ships Docker + Compose plugin preinstalled).
3. **Choose a plan:** see the [sizing table](#recommended-droplet-sizes). Paid-pilot baseline is **Basic $48/mo (8 GB / 4 vCPU)**.
4. **Datacenter region:** close to your users (latency) and your operator team (admin convenience).
5. **Authentication:** select your SSH key. Never enable password auth.
6. **Hostname:** something descriptive like `validibot-prod`.
7. **Create Droplet** and note the public IPv4 address.

### Using doctl

```bash
# Install doctl: https://docs.digitalocean.com/reference/doctl/how-to/install/

doctl compute droplet create validibot-prod \
  --image docker-20-04 \
  --size s-4vcpu-8gb \
  --region syd1 \
  --ssh-keys $(doctl compute ssh-key list --format ID --no-header | head -1) \
  --enable-monitoring \
  --wait
```

Adjust `--size` and `--region` to match your sizing decision. `--enable-monitoring` installs the DigitalOcean Monitoring agent — useful for graphs and alerts, optional if you'd rather keep telemetry off.

## Step 2: Attach block-storage volume

For paid pilots and anything in production, application data and Postgres files should live on a separate block-storage volume — not the boot disk. This means Droplet rebuilds (or resizes) don't destroy your data, and backups become an independent object.

1. **Create → Volumes**.
2. Same region as the Droplet.
3. **Size:** 200 GB for a paid pilot. Bump to 500 GB+ for heavy simulation workloads.
4. **Choose a Droplet to attach to:** the one you just created.
5. **Filesystem:** ext4.
6. **Mount path:** `/srv/validibot`.

DigitalOcean's UI generates the exact `mkfs`, `mkdir`, `/etc/fstab`, and `mount` commands. Run them on the Droplet as root and confirm:

```bash
df -h /srv/validibot
# Should show your volume mounted, ~200 GB available
```

The bootstrap step ([Step 6](#step-6-bootstrap-the-host)) will create `repo/`, `data/`, and `backups/` subdirectories under this mount with the right ownership.

## Step 3: Configure DNS

Add an A-record at your DNS provider pointing your chosen hostname (e.g. `validibot.example.org`) at the Droplet's public IPv4 address. TTL 3600 is fine.

You can host DNS at DigitalOcean (**Networking → Domains**) or at any other registrar — Validibot doesn't care.

Verify propagation from your laptop:

```bash
dig +short validibot.example.org
# Should print the Droplet IP
```

After [Step 6](#step-6-bootstrap-the-host), you'll re-verify from the Droplet itself with `just self-hosted check-dns`. Doing that *before* enabling TLS prevents Caddy from burning Let's Encrypt rate-limit attempts against bad DNS.

## Step 4: Configure DigitalOcean Cloud Firewall

DigitalOcean Droplets ship with UFW installed but disabled. Do not enable UFW for this deployment: **Docker bypasses UFW by default**, which silently exposes container ports you thought you'd blocked. Use a DigitalOcean Cloud Firewall instead — it filters at the network edge before traffic reaches the Droplet.

1. **Networking → Firewalls → Create Firewall**.
2. Name: `validibot-prod`.
3. **Inbound rules:**
   - **SSH (TCP 22)** — *Sources:* your operator IP/CIDR only. Don't allow `0.0.0.0/0` here.
   - **HTTP (TCP 80)** — *Sources:* all IPv4 + IPv6. Caddy needs port 80 for Let's Encrypt ACME challenges.
   - **HTTPS (TCP 443)** — *Sources:* all IPv4 + IPv6.
   - Everything else: omit (default-deny).
4. **Outbound rules:** allow all (the default).
5. **Apply to Droplets:** select `validibot-prod`.
6. **Create Firewall**.

## Step 5: SSH hardening and non-root user

SSH to the Droplet as root for the last time:

```bash
ssh root@<droplet-ip>
```

### Create the non-root user

```bash
adduser validibot
usermod -aG sudo validibot
usermod -aG docker validibot

# Copy your SSH key from root to the new user
rsync --archive --chown=validibot:validibot ~/.ssh /home/validibot
```

### Harden sshd

Edit `/etc/ssh/sshd_config` and ensure:

```text
PasswordAuthentication no
PermitRootLogin prohibit-password
```

Then `systemctl restart sshd`.

### Install Fail2Ban (defence in depth)

```bash
apt update && apt install -y fail2ban
systemctl enable --now fail2ban
```

Test that the new user works *before* logging out of the root session:

```bash
# From a SECOND terminal on your laptop:
ssh validibot@<droplet-ip>
```

Once that works, exit the root session.

## Step 6: Bootstrap the host

There's a Validibot helper that installs Docker (if not already present), creates the `validibot` user (idempotent), installs `just`, and sets up the data directories under `/srv/validibot/`.

> **Status note:** the bootstrap scripts (`bootstrap-host`, `bootstrap-digitalocean`) currently print "not yet implemented" — they're Phase-0 stubs of [ADR-2026-04-27](https://github.com/danielmcquillen/validibot-project). The fallback steps below are what they will eventually automate. Once the scripts ship, the fallback section will be removed from this guide.

### When the scripts are implemented (target state)

```bash
ssh validibot@<droplet-ip>
sudo git clone https://github.com/validibot/validibot.git /srv/validibot/repo
sudo chown -R validibot:validibot /srv/validibot/repo
cd /srv/validibot/repo
./deploy/self-hosted/scripts/bootstrap-digitalocean --data-root /srv/validibot
```

That script will detect the Droplet (via the metadata service), validate the volume mount, install `just`, and prep the directory tree.

### Today (manual fallback)

```bash
ssh validibot@<droplet-ip>

# Install just (used to drive all subsequent operations)
sudo curl -sSL https://github.com/casey/just/releases/latest/download/just-1.36.0-x86_64-unknown-linux-musl.tar.gz \
  | sudo tar -xz -C /usr/local/bin just

# Clone the repo into the volume so the working tree survives Droplet rebuilds
sudo git clone https://github.com/validibot/validibot.git /srv/validibot/repo
sudo chown -R validibot:validibot /srv/validibot/repo

# Set up the data directories
sudo mkdir -p /srv/validibot/data /srv/validibot/backups
sudo chown -R validibot:validibot /srv/validibot/data /srv/validibot/backups

cd /srv/validibot/repo
```

The Docker marketplace image already installed Docker Engine + Compose plugin, so there's nothing else to do at the host level.

## Step 7: Configure environment files

Validibot uses three env files for self-hosted deployments — all under `.envs/.production/.self-hosted/`:

| File | Purpose |
|---|---|
| `.django` | Django runtime config — secrets, allowed hosts, email, storage paths, MFA key. |
| `.postgres` | Postgres credentials. |
| `.build` | Build-time config — image tags, optional Pro package URL. |

Copy the templates and edit them:

```bash
cp -r .envs.example/.production/.self-hosted/ .envs/.production/.self-hosted/

$EDITOR .envs/.production/.self-hosted/.django
$EDITOR .envs/.production/.self-hosted/.postgres
$EDITOR .envs/.production/.self-hosted/.build
```

The `.django` file is organised into eight sections. The required settings for a fresh install:

```bash
# Section 1: required
SITE_URL=https://validibot.example.org
DJANGO_ALLOWED_HOSTS=validibot.example.org
DJANGO_SECRET_KEY=<see below>
DJANGO_MFA_ENCRYPTION_KEY=<see below>

# Section 2: URLs/security
DJANGO_CSRF_TRUSTED_ORIGINS=https://validibot.example.org
DJANGO_SECURE_SSL_REDIRECT=false   # Caddy terminates TLS; Django serves plain HTTP behind it

# Section 4: storage
DATA_STORAGE_ROOT=/srv/validibot/data

# Section 5: email — configure your provider (Mailgun, SES, SMTP relay)
DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
DJANGO_DEFAULT_FROM_EMAIL=validibot@example.org
# ... your SMTP creds ...

# Initial admin
SUPERUSER_USERNAME=admin
SUPERUSER_EMAIL=admin@example.org
SUPERUSER_PASSWORD=<strong password>
```

Generate the two secrets:

```bash
# DJANGO_SECRET_KEY
docker run --rm python:3.13-alpine python -c \
  "from secrets import token_urlsafe; print(token_urlsafe(50))"

# DJANGO_MFA_ENCRYPTION_KEY (must be Fernet-format)
docker run --rm python:3.13-alpine sh -c \
  "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
```

And in `.postgres`, set a strong password:

```bash
POSTGRES_PASSWORD=<32+ random chars>
```

Validate before going further:

```bash
just self-hosted check-env
just self-hosted check-dns   # verifies SITE_URL resolves to this Droplet's public IP
```

`check-env` reports missing required vars and unchanged placeholders. `check-dns` is your last chance to catch a DNS typo before Let's Encrypt rate-limits you.

## Step 8: Start the stack

First, bring everything up without TLS — useful sanity check that the app boots and migrations succeed.

```bash
just self-hosted bootstrap
```

`bootstrap` builds the image, runs migrations, creates the superuser (from `SUPERUSER_*` vars), registers OIDC clients, seeds default validators and roles, and starts the stack.

Then enable the bundled Caddy reverse proxy with auto-TLS:

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

Caddy requests a Let's Encrypt certificate for `SITE_URL` on first boot. If DNS resolves correctly (verified by Step 7) and ports 80/443 are open (Step 4), you'll have a valid cert within a few seconds.

### If you already run a reverse proxy

Don't enable the `caddy` profile. Configure your existing proxy (nginx, Traefik, Cloudflare Tunnel, hosting-provider load balancer) to forward to the `web` container on port 8000, and set the following in `.django`:

```bash
DJANGO_SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https
DJANGO_CSRF_TRUSTED_ORIGINS=https://validibot.example.org
```

## Step 9: Verify the deployment

```bash
just self-hosted status         # all containers Up + healthy?
just self-hosted health-check   # quick HTTP health probe
just self-hosted doctor         # full diagnostic
just self-hosted doctor --provider digitalocean   # + DO-specific checks
just self-hosted smoke-test     # end-to-end demo workflow
```

> `doctor` is partially implemented (Phase 1, in progress). `smoke-test` is Phase 2 — it currently prints a "not yet implemented" message. Run them anyway so you can see which checks are live today.

From your laptop:

```bash
curl -I https://validibot.example.org/health/
# HTTP/2 200
# server: Caddy
# ...
```

And in the browser at `https://validibot.example.org`: you should see the Validibot login screen with a valid certificate. Log in with the superuser credentials from `.django`.

### What to check before declaring success

- All containers are `running` and `healthy` in `just self-hosted status`.
- HTTPS resolves with a valid cert (no browser warnings).
- The doctor command reports `OK`, `INFO`, `WARN`, or `SKIPPED` only — zero `ERROR` or `FATAL`.
- The DO-specific overlay (`--provider digitalocean`) confirms DNS, volume mount, monitoring agent, and Cloud Firewall posture.
- You can log in as the superuser and create a test workflow.

## Step 10: Configure automated backups

Validibot's eventual story is a single `just self-hosted backup` recipe that produces a manifest-tagged tarball under `backups/`. That's Phase 3 work and not implemented today; the working approach right now is a database-only cron job plus a periodic restic snapshot of the data directory.

### Today's working approach: database dumps + restic for data

```bash
# Create the backup script
sudo tee /home/validibot/backup-validibot.sh > /dev/null <<'EOF'
#!/bin/bash
set -e
BACKUP_DIR=/srv/validibot/backups
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

cd /srv/validibot/repo
docker compose -f docker-compose.production.yml exec -T postgres \
  pg_dump -U validibot validibot | gzip > "$BACKUP_DIR/db_$DATE.sql.gz"

# Retain 14 days locally; off-host retention is handled by restic
find "$BACKUP_DIR" -name "db_*.sql.gz" -mtime +14 -delete

echo "Backup completed: db_$DATE.sql.gz"
EOF
sudo chmod +x /home/validibot/backup-validibot.sh
sudo chown validibot:validibot /home/validibot/backup-validibot.sh

# Run daily at 03:00 as the validibot user
(crontab -u validibot -l 2>/dev/null; echo "0 3 * * * /home/validibot/backup-validibot.sh >> /srv/validibot/backups/backup.log 2>&1") | sudo crontab -u validibot -
```

For off-host encrypted backups (strongly recommended), install [restic](https://restic.net/) and point it at the `/srv/validibot/` directory, sending the repository to S3, B2, GCS, Azure, or DigitalOcean Spaces. Restic gives you deduplication, encryption, and retention policies. The Validibot [Backups doc](../backups.md) covers patterns in more depth.

### What DigitalOcean snapshots are good for

DigitalOcean **Droplet snapshots** and **automatic backups** are infrastructure-level — they roll the whole VM back to a point in time. They're useful for catastrophic recovery (the Droplet itself is corrupted), but they **do not replace** application-level backups, because:

- They can't produce a `manifest.json` with migration state and checksums.
- They can't pass through Validibot's restore compatibility check.
- They can't be partially restored (e.g. just the database).
- They snapshot the *whole disk* — including things you don't want, like in-flight task state.

Enable DigitalOcean automatic Droplet backups if you want — they're cheap insurance — but treat them as *additional to*, not a substitute for, application-level backups. See [Restore](../restore.md) for the full restore-vs-snapshot discussion.

### Target state (Phase 3)

```bash
just self-hosted backup            # full application backup with manifest
just self-hosted backup --dry-run  # show what would be backed up
```

Output structure when implemented:

```text
/srv/validibot/backups/
  2026-04-27T120000Z/
    manifest.json
    database.sql.zst
    data.tar.zst
    checksums.sha256
```

## Step 11: Perform a restore drill

Backups you haven't restored are wishes, not backups. Before you call the install production-ready, restore one onto a clean test Droplet and verify it boots.

### Today

1. Create a second Droplet of the same size in the same region.
2. Follow Steps 1-8 of this guide to get a fresh Validibot stack running.
3. SCP your most recent `db_*.sql.gz` to the test Droplet.
4. `docker compose -f docker-compose.production.yml exec -T postgres dropdb -U validibot validibot && createdb -U validibot validibot`
5. `gunzip -c db_TIMESTAMP.sql.gz | docker compose -f docker-compose.production.yml exec -T postgres psql -U validibot validibot`
6. Run `just self-hosted doctor` and `just self-hosted health-check`. Log in. Confirm your data is there.

### Target state (Phase 3)

```bash
just self-hosted bootstrap
just self-hosted restore /path/to/backups/2026-04-27T120000Z
just self-hosted doctor
just self-hosted smoke-test
```

Doctor warns (check `VB411`) if a backup has never been restored. Make sure that warning is gone before you treat the install as production-ready.

## Step 12: Upgrade workflow

Upgrades follow a fixed sequence: back up, pull, rebuild, migrate, restart, verify.

### Today

```bash
cd /srv/validibot/repo

# Always back up before upgrading
/home/validibot/backup-validibot.sh

git pull origin main
just self-hosted update     # pulls, rebuilds, migrates, restarts

just self-hosted health-check
just self-hosted logs-service web   # check for errors
```

### Target state (Phase 4)

```bash
just self-hosted backup
just self-hosted upgrade --to v0.9.0
just self-hosted doctor
just self-hosted smoke-test
```

The Phase-4 `upgrade` recipe will enforce strict upgrade paths (no skipping a major version), surface migration plans before applying them, and handle in-flight validation runs gracefully.

## Step 13: Optional enhancements

### Managed PostgreSQL

For production workloads at scale, offload Postgres to DigitalOcean's managed database service. Benefits: automatic backups, failover options, easier scaling, and Postgres patches handled for you.

1. **Databases → Create Database Cluster** → PostgreSQL 16, same region as your Droplet.
2. Basic plan ($15/mo) suffices for most installs. Add a standby node ($30/mo total) for HA.
3. **Trusted sources:** add your Droplet (lock the cluster down so only it can connect).
4. Edit `.envs/.production/.self-hosted/.postgres`:

   ```bash
   POSTGRES_HOST=your-cluster.db.ondigitalocean.com
   POSTGRES_PORT=25060
   POSTGRES_DB=validibot
   POSTGRES_USER=validibot
   POSTGRES_PASSWORD=<from the DO control panel>
   POSTGRES_OPTIONS=?sslmode=require
   ```

5. Remove (or override) the `postgres` service in `docker-compose.production.yml` so you're not running two databases.
6. `just self-hosted deploy` to apply the change.

### DigitalOcean Spaces (object storage)

DigitalOcean Spaces is S3-compatible. Validibot's S3 storage backend isn't implemented yet, so for now:

- Stay on the default local volume storage (with the volume mounted at `/srv/validibot`, you're already on durable storage).
- Or use the GCS-backed deployment path if you genuinely need object storage today.

Do **not** set `DATA_STORAGE_BACKEND=s3` in a current Validibot deployment.

### DigitalOcean Monitoring agent

If you didn't pass `--enable-monitoring` at Droplet creation, install the agent now:

```bash
curl -sSL https://repos.insights.digitalocean.com/install.sh | sudo bash
```

The agent reports CPU, memory, disk, and network metrics back to DigitalOcean for the **Graphs** tab and alert policies. It's free. Validibot's `doctor --provider digitalocean` check (VB912) reports whether it's installed.

### Advanced validators (EnergyPlus, FMU, custom)

Advanced validators run as short-lived sibling containers spawned by the worker via the host Docker socket — already configured in `docker-compose.production.yml`. To use them:

1. **Pre-pull the validator images** so the first run isn't bottlenecked on download:

   ```bash
   docker pull ghcr.io/validibot/validator-energyplus:latest
   docker pull ghcr.io/validibot/validator-fmu:latest
   ```

2. **Private registries:** authenticate Docker on the Droplet:

   ```bash
   docker login ghcr.io -u USERNAME -p TOKEN
   ```

3. **Network isolation:** by default validator containers run with no network access. If a validator legitimately needs to fetch external resources, set `VALIDATOR_NETWORK` in `.django`.

See [Validator Images](../validator-images.md) for image pinning, registry auth, and isolation details.

## Step 14: (optional) Activate Pro

If you've bought a Pro license, you'll have received a private package URL and a wheel reference. Activation is two config changes plus a redeploy — no data migration, no separate install:

1. Edit `.envs/.production/.self-hosted/.build`:

   ```bash
   VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro==<version>
   VALIDIBOT_PRIVATE_INDEX_URL=https://<email>:<token>@pypi.validibot.com/simple/
   ```

2. Edit `.envs/.production/.self-hosted/.django`:

   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.production_pro
   ```

3. Redeploy:

   ```bash
   just self-hosted deploy
   just self-hosted doctor
   ```

Pro migrations are additive — they create new tables (teams, guests, signed credentials, MCP, advanced analytics) but never reshape community tables. Existing data, users, workflows, and runs are preserved.

The private package URL is the licensing gate. There is no runtime phone-home. A running Pro instance keeps running even if outbound internet is cut off. License expiry blocks future package downloads (your next upgrade will fail), not the running install.

## Troubleshooting

### A container won't start

```bash
just self-hosted status              # which container is unhealthy?
just self-hosted logs-service web    # what does it say?
just self-hosted logs-service postgres
```

Most common causes:

- **Database connection refused:** `POSTGRES_*` env vars don't match, or Postgres hasn't finished initialising yet. Wait 10 seconds and retry. If using Managed Postgres, confirm trusted-source rules include the Droplet.
- **Permission denied on `/var/run/docker.sock`:** the `validibot` user isn't in the `docker` group. `groups validibot` should include `docker`. If not, `usermod -aG docker validibot` and log out/in.
- **Permission denied on `/srv/validibot/data`:** `chown -R validibot:validibot /srv/validibot/data`.

### TLS / Let's Encrypt errors

```bash
just self-hosted logs-service caddy
```

Common causes:

- **DNS not propagated:** `dig +short validibot.example.org` from the Droplet should return the Droplet's IP. If not, wait and retry.
- **Port 80 blocked:** Cloud Firewall is missing the HTTP inbound rule. Caddy needs port 80 for the ACME HTTP-01 challenge even if you only serve HTTPS afterwards.
- **Rate limited:** Let's Encrypt rate-limits failed attempts. Wait an hour. Don't keep retrying with broken DNS — that's exactly what `check-dns` is for.

### Database connection refused (Managed Postgres)

```bash
# From the Droplet, try connecting directly:
psql "postgresql://validibot:PASSWORD@your-cluster.db.ondigitalocean.com:25060/validibot?sslmode=require"
```

If this fails: trusted sources don't include the Droplet, the password is wrong, or `sslmode=require` is missing.

### Out of memory

```bash
free -h
docker stats --no-stream
```

Quick fix (buys you headroom for one advanced validator run):

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Long-term fix: resize the Droplet up a tier, or move PostgreSQL to Managed Postgres to free ~300 MB.

### "doctor" or "smoke-test" reports "not yet implemented"

That's expected today. These commands ship across ADR phases 1-2. The recipe names exist so the operator surface is visible; the implementations land progressively. See `just self-hosted --list` for which recipes do real work today.

## Final install report

When you've completed all twelve steps, capture these for your team's wiki so you (or whoever's on call next) can answer "what's running where?" in seconds:

- Droplet size, region, and IP.
- Volume size and mount point.
- Validibot version (`git rev-parse HEAD` in `/srv/validibot/repo`) and image digests.
- DNS hostname.
- Backup destination (local path + off-host bucket / restic repo).
- Latest `doctor --json` output.
- Latest smoke-test report.
- Restore-drill date and outcome.
- Pro license details (if applicable) and entitlement expiry.

## See also

- [Self-Hosting Overview](../overview.md) — concepts, two editions, day-to-day operations
- [Install](../install.md) — substrate-generic install steps (AWS EC2, Hetzner, on-prem)
- [Backups](../backups.md) — backup architecture and off-host patterns
- [Restore](../restore.md) — restore drills and the snapshot-vs-backup distinction
- [Security Hardening](../security-hardening.md) — full hardening checklist
- [Pilot Onboarding](../pilot-onboarding.md) — checklist for joint customer install reviews
- [Operator Recipes](../operator-recipes.md) — full `just self-hosted` recipe reference
- [Doctor Check IDs](../doctor-check-ids.md) — every diagnostic check ID and its fix

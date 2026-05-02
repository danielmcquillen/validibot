# Self-Hosting Validibot on DigitalOcean

> **Status:** Phase 0 outline. The full ten-step tutorial is in
> progress. Today this page documents the structure and the commands
> the tutorial will end on, so operators can see what they're
> committing to before the full guide ships.

## Goal

Get a working self-hosted Validibot instance on a DigitalOcean
Droplet, running behind your own domain, with TLS, backups, and a
documented restore drill.

## Prerequisites

- A DigitalOcean account with billing set up.
- A registered domain you can add an A-record to (e.g.
  `validibot.example.org`).
- An SSH keypair for Droplet access.
- A laptop with `git`, `ssh`, and `just` installed.
- Optional: a Validibot Pro license if you want signed credentials,
  teams, guests, or MCP. Community works without one.

## Recommended architecture

| Use case | Droplet | Storage | Database | Notes |
|---|---|---|---|---|
| Evaluation | 2 vCPU / 4 GB | Boot disk only | Compose Postgres | Short-lived trial, no promises |
| **Paid pilot** | **4 vCPU / 8-16 GB** | **200 GB volume mounted at `/srv/validibot`** | **Compose Postgres** | **Recommended default** |
| Heavier simulation | 8+ vCPU / 32 GB | 500 GB+ volume | Compose or managed Postgres | Treat as a paid deployment review |

The paid-pilot row is what most early customers should pick.

What DigitalOcean owns:

- VM provisioning, networking, block storage, snapshots, optional
  managed Postgres, Cloud Firewall.

What Validibot owns:

- Compose configuration, app secrets, migrations, validation data,
  evidence bundles, application backups, restore tests, upgrades, and
  support bundles.

## The ten steps (outline only; full walkthrough in Phase 1)

### Step 1: Create the Droplet

Ubuntu LTS, in your customer's preferred region, with SSH-key login
(no passwords). Pick the size from the recommended-architecture table
above.

### Step 2: Attach storage

For paid pilots: create a 200 GB block-storage volume in the same
region and attach it. Mount it at `/srv/validibot`. Format ext4 if
prompted. This is where Validibot data, evidence, and Postgres files
will live — keep them off the boot disk.

### Step 3: Configure DNS and firewall

DNS: add an A-record for `validibot.example.org` pointing at the
Droplet's public IP. Wait for the TTL.

Firewall: create a DigitalOcean Cloud Firewall on the Droplet:

- allow `22/tcp` from your operator IP range only;
- allow `80/tcp` and `443/tcp` from the internet;
- deny everything else inbound (default-deny).

Validate from the Droplet (after bootstrap, when `just` is installed):

```bash
just self-hosted check-dns
# Resolves SITE_URL via dig, compares to this host's public IP.
```

### Step 4: Bootstrap the host

SSH to the Droplet as root and run:

```bash
git clone https://github.com/validibot/validibot.git /srv/validibot/repo
cd /srv/validibot/repo
./deploy/self-hosted/scripts/bootstrap-digitalocean --data-root /srv/validibot
# Detects the Droplet, validates the volume mount, installs Docker
# and just, creates the validibot user and dirs. Phase 0: stub.
```

After bootstrap finishes, switch to the `validibot` user — root has
nothing more to do:

```bash
exit
ssh validibot@<droplet-ip>
cd /srv/validibot/repo
```

From here on, everything is `just self-hosted <recipe>`.

### Step 5: Configure Validibot

```bash
cp .envs.example/.production/.self-hosted/.django \
   .envs/.production/.self-hosted/.django
cp .envs.example/.production/.self-hosted/.postgres \
   .envs/.production/.self-hosted/.postgres
cp .envs.example/.production/.self-hosted/.build \
   .envs/.production/.self-hosted/.build

$EDITOR .envs/.production/.self-hosted/.django
# Set SITE_URL, DJANGO_ALLOWED_HOSTS, DJANGO_SECRET_KEY, MFA key,
# email provider, etc. The file has section headers matching the eight
# ADR groups (required, URLs/security, database/cache, storage, email,
# validators, Pro/signing, optional telemetry).

just self-hosted check-env
# Verifies required vars are set and placeholder values are gone.
```

### Step 6: Start Validibot

```bash
# Without TLS first (port 8000 only) — useful for sanity check
just self-hosted bootstrap

# Then enable Caddy with auto-TLS
COMPOSE_PROFILES=caddy just self-hosted deploy
```

Caddy requests a Let's Encrypt cert for `SITE_URL`. If DNS is wrong,
this fails AND counts against the LE rate limit. Step 3's `check-dns`
is what prevents that.

### Step 7: Run doctor and smoke test

```bash
just self-hosted doctor          # Phase 1
just self-hosted smoke-test      # Phase 2
```

Doctor reports `[OK] / [WARN] / [ERROR]` per check, with actionable
fix suggestions. Smoke test creates a demo org, runs a small
validation, exports an evidence bundle, and reports pass/fail.

In Phase 0 these print "not yet implemented" stubs. The full output
schema is documented in ADR section 6 (doctor) and section 7 (smoke
test).

### Step 8: Configure backups

```bash
just self-hosted backup --dry-run    # Phase 3
just self-hosted backup              # Phase 3
```

Backup output:

```text
backups/
  2026-04-27T120000Z/
    manifest.json
    database.sql.zst
    data.tar.zst
    checksums.sha256
```

Recommended: configure restic for off-host encrypted backups.
DigitalOcean Droplet snapshots and automatic backups are useful for
infrastructure recovery but **do not replace** application-level
backup, because Validibot needs an application manifest with
checksums and migration state.

### Step 9: Perform a restore drill

```bash
# On a clean test Droplet:
just self-hosted bootstrap
just self-hosted restore /path/to/backups/2026-04-27T120000Z
just self-hosted doctor
just self-hosted smoke-test
```

Doctor warns if a backup has never been restored — make sure the
warning goes away before you call the install production-ready.

### Step 10: Upgrade and support workflow

```bash
just self-hosted backup         # Required before upgrade
just self-hosted upgrade --to v0.9.0   # Phase 4
just self-hosted doctor
just self-hosted smoke-test
```

For paid Pro support:

```bash
just self-hosted collect-support-bundle    # Phase 6
# Email the resulting .zip to support@validibot.com.
```

The bundle excludes secrets, signing keys, and raw submission
contents.

## Final install report

Record these on completion (paste them into your team's wiki):

- Droplet size and region
- Validibot version and image digests
- Data volume mount point
- Backup destination (off-host bucket / restic repo)
- Latest doctor JSON
- Latest smoke-test report
- Restore-drill timestamp

## Troubleshooting

(Phase 1 work — placeholder.) Common issues will be documented after
the first DigitalOcean install walkthrough with a real operator.

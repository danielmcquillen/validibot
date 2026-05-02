# Security Hardening

This page is the practical checklist for hardening a self-hosted Validibot install. It captures the security model the trust-boundary ADR established and the recommended hardening from the boring-self-hosting ADR.

If you're a risk-averse customer (energy modeling consultancy, research lab, utility reviewer), running through this list is part of going from "deployed" to "production-ready."

## The security model

The default self-hosted profile assumes:

- **customer controls the VM** — Validibot does not require root or kernel-level privileges beyond what Docker needs;
- **customer controls DNS and TLS** — bring your own certificates or use the bundled Caddy profile;
- **customer controls backups** — Validibot ships the recipes but you own where they go;
- **Validibot containers are trusted** application code;
- **validator containers are semi-trusted** and isolated per run (see [Validator Images](validator-images.md));
- **users may upload untrusted files** — the launch contract validates them at the boundary;
- **admins may install additional validator images** — once self-service registration ships, those go through tier-2 hardening;
- **outbound internet may be restricted** — Validibot does not phone home by default.

## Recommended hardening

### 1. Run behind HTTPS

Use Caddy (bundled) or your own reverse proxy. The kit's Caddyfile uses `SITE_URL` to provision Let's Encrypt certificates on startup.

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

If you bring your own proxy:

- forward to the `web` container on port 8000;
- set `DJANGO_SECURE_PROXY_SSL_HEADER` appropriately in `.django`;
- set `DJANGO_CSRF_TRUSTED_ORIGINS` to include your public origin;
- set `DJANGO_SECURE_SSL_REDIRECT=true`;
- enable HSTS via `DJANGO_SECURE_HSTS_*` settings.

### 2. Use strong generated secrets

```bash
# DJANGO_SECRET_KEY
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# DJANGO_MFA_ENCRYPTION_KEY (must be Fernet-format)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The doctor command (`VB001` family) checks for missing or development-default secrets.

### 3. Keep Postgres and Redis private to the Compose network

The default `docker-compose.production.yml` only exposes the `web` container's port (and Caddy's 80/443 if enabled). Postgres and Redis are accessible only inside the Compose network.

If you bind Postgres to the host (e.g. for an external admin tool), bind to `127.0.0.1`, not `0.0.0.0`. Use a SSH tunnel for remote admin access.

### 4. Pin Validibot and validator images by version

Production docs recommend exact-version tags or digest pins, not `latest`.

```bash
# .envs/.production/.self-hosted/.build
VALIDIBOT_IMAGE_TAG=0.8.0          # exact version
VALIDATOR_IMAGE_POLICY=digest       # or 'signed-digest' once Phase 5 ships
```

The doctor command warns if `latest` is used in a `self-hosted` or `self-hosted-hardened` profile.

### 5. Run-scoped validator mounts (default after trust ADR Phase 1)

Validator backends get only their own per-run input/output directories, not the global storage root. See [Validator Images](validator-images.md) for the full layout.

This is a **default**, not a configurable hardening — the old global mount has been removed entirely. The negative-control isolation test in CI would fail if it were re-introduced.

### 6. Disable validator network access by default

Validator containers run with `network_disabled=True` by default. Only validators that explicitly declare `requires_network: true` in their manifest get network access, and the deployment-side policy can override that to refuse network for any validator.

For `self-hosted-hardened` profile: refuse network for all validators globally. The runtime policy must not depend on trusting the image's self-description.

### 7. Use rootless Docker or Podman where feasible

The default Docker daemon runs as root. Rootless Docker (Docker 20.10+) or rootless Podman runs the daemon as a non-root user. Significant security improvement for handling untrusted validator containers.

The kit documents both as **optional** hardening, not required for MVP. Setup notes are in `deploy/self-hosted/scripts/bootstrap-host` (rootless variant).

Doctor's compatibility-matrix check (VB320) reports the running Docker version and detects whether rootless is in use.

### 8. Back up database and data storage off-host

The `backups/` directory is on the same VM by default. For production, copy backups off-host. Options: rsync to another machine, restic to S3-compatible storage, cloud provider snapshots (as a complement, not a replacement). See [Backups](backups.md).

### 9. Test restore quarterly

A backup that has never been restored is not considered valid. The doctor command warns if no restore-test marker is recorded (VB411). See [Restore](restore.md) for the drill.

### 10. Keep telemetry off unless explicitly desired

Self-hosted Validibot is **telemetry-off by default**. No product analytics. No license phone-home. No usage reporting.

Allowed outbound calls by default:

- container image pulls during install/upgrade;
- email delivery if configured;
- Let's Encrypt ACME challenges if Caddy profile is enabled.

Sentry error reporting and other diagnostics can be opted in via `.envs/.production/.self-hosted/.django`. None are required.

## What outbound calls happen

The doctor command on self-hosted reports which outbound calls are enabled, so operators can audit. Section by section:

| Outbound call | Default | Purpose |
|---|---|---|
| Container image pulls | enabled | Install and upgrade |
| Email delivery | configured | Outbound app email if you set an SMTP backend |
| Let's Encrypt ACME | enabled if Caddy profile is on | TLS certs |
| Sentry error reporting | off | Optional opt-in for diagnostics |
| PostHog product analytics | off | Not enabled in self-hosted |
| Pro license phone-home | off | Package-index credential is the entitlement gate, not a runtime call |
| x402 public agent registry | off | Cloud-only feature |

## Hardened profile

For risk-averse customers, the `self-hosted-hardened` profile applies stricter defaults:

- `VALIDATOR_IMAGE_POLICY=signed-digest` (when Phase 5 lands);
- all telemetry off;
- no runtime license phone-home;
- local signing/JWKS checks;
- rootless/socket proxy docs treated as required, not optional.

Set with `DEPLOYMENT_PROFILE=self-hosted-hardened` in `.django`.

## Filesystem permissions

The `bootstrap-host` script sets these permissions during install. If you skipped it or set up Docker yourself, verify:

| Path | Owner | Mode |
|---|---|---|
| `/srv/validibot/data/` | `1000:1000` | `755` |
| `/srv/validibot/data/runs/` | `1000:1000` | `755` |
| `/srv/validibot/data/evidence/` | `1000:1000` | `755` |
| `/srv/validibot/data/backups/` | `1000:1000` | `750` |
| `.envs/.production/.self-hosted/` | `1000:1000` | `700` |

Doctor's `VB201` check verifies the data root is writable by the app and not by root only.

## Network architecture

Recommended firewall configuration:

| Port | Source | Purpose |
|---|---|---|
| `22/tcp` | operator IP ranges only | SSH |
| `80/tcp` | internet | HTTP (redirects to HTTPS) |
| `443/tcp` | internet | HTTPS |
| `5432/tcp` | none (or 127.0.0.1 only) | Postgres |
| `6379/tcp` | none | Redis |
| `5555/tcp` | none | Flower (if enabled) |
| `8000/tcp` | none (proxied via Caddy/your proxy) | Web |

DigitalOcean's Cloud Firewall is documented in [providers/digitalocean.md](providers/digitalocean.md) with these rules. Other providers: configure equivalently.

## Audit log

Validibot writes audit events for trust-relevant actions:

- workflow access denied;
- workflow execution denied;
- launch rejected by file-type contract;
- launch rejected by step incompatibility;
- validator sandbox policy violation;
- evidence bundle exported;
- evidence bundle export omitted raw content due to retention policy.

The full audit log architecture is documented in the (founder-facing) [audit-log doc](https://github.com/danielmcquillen/validibot-project/blob/main/docs/observability/audit-log.md). Self-hosted operators can query the audit log via the admin UI or the database directly.

## Incident response

If you suspect compromise:

1. Generate a support bundle: `just self-hosted collect-support-bundle`. The bundle is redacted (no secrets, no raw submission contents).
2. Email support@validibot.com with the bundle attached. Pro Team gets 24-hour response; Research/Studio and Organization tiers get 4-hour response.
3. If the issue is severe, take the instance offline (`just self-hosted stop`) and preserve the data/database for forensics.
4. Restore from the most recent uncompromised backup (see [Restore](restore.md)).

The support bundle is the trust contract: if a customer can't trust that sending it preserves their data custody, they won't send it. See [Support Bundle](support-bundle.md) for what's included and what's redacted.

## See also

- [Install](install.md) — initial setup
- [Validator Images](validator-images.md) — run-scoped isolation
- [Backups](backups.md) — off-host backup recommendation
- [Restore](restore.md) — quarterly drill
- [Support Bundle](support-bundle.md) — what's redacted
- [Doctor Check IDs](doctor-check-ids.md) — security-relevant checks
- [Trust Architecture (developer-facing)](../../dev_docs/overview/trust-architecture.md)

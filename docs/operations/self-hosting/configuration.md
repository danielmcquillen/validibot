# Configuration Reference

This page is the env-file and settings-module reference for self-hosted Validibot. It documents the eight grouped sections of `.envs/.production/.self-hosted/.django`, what each setting controls, and how the deployment profile model selects defaults.

For the install flow, see [Install](install.md).

## File layout

```text
.envs/.production/.self-hosted/
  .django           # Django app settings, security, validators, Pro/signing
  .postgres         # Postgres credentials and tuning
  .build            # Image versions, package index URLs
  .mcp              # MCP server settings (Pro feature)
```

You copy these from `.envs.example/.production/.self-hosted/` once during install. They live outside source control.

## `.django` — the eight grouped sections

The `.django` file is structured into eight comment-headered sections matching the boring-self-hosting ADR's grouping:

### 1. Required

Settings that must be set before the app starts.

| Setting | Purpose |
|---|---|
| `SITE_URL` | The public URL of your Validibot instance (e.g. `https://validibot.example.org`). Used everywhere — emails, OIDC, callback URLs, evidence verification URLs. |
| `DJANGO_SECRET_KEY` | Django's signing key. Generate with `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`. |
| `ALLOWED_HOSTS` | Comma-separated list of domain names this instance serves. Must include the host portion of `SITE_URL`. |
| `DJANGO_MFA_ENCRYPTION_KEY` | Fernet key for encrypting TOTP secrets. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `DJANGO_SETTINGS_MODULE` | `config.settings.production` for community, `config.settings.production_pro` for Pro. |

The doctor command's VB001-VB099 range checks these.

### 2. URLs and security

| Setting | Purpose |
|---|---|
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Origins allowed for CSRF-protected POSTs. Include your `SITE_URL` and any other origins that hit Validibot's API. |
| `DJANGO_SECURE_SSL_REDIRECT` | `true` for production. Redirects HTTP → HTTPS. |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | Set if you have a reverse proxy terminating TLS. Format: `HTTP_X_FORWARDED_PROTO,https`. |
| `DJANGO_SECURE_HSTS_SECONDS` | HSTS max-age. Recommended: `31536000` (one year). |
| `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` | `true` if all subdomains use HTTPS. |
| `DJANGO_SECURE_HSTS_PRELOAD` | `true` to opt into HSTS preload (irreversible — read the docs first). |

### 3. Database and cache

| Setting | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string. Default: the bundled Compose Postgres. |
| `REDIS_URL` | Redis connection string. Default: the bundled Compose Redis. |
| `CACHE_BACKEND` | Cache backend. Default: Redis. Self-hosted can use `DatabaseCache` if Redis is unavailable. |

### 4. Storage

| Setting | Purpose |
|---|---|
| `DATA_STORAGE_ROOT` | Root directory for all persistent data: runs, evidence, validator resources, uploads. Default: `/srv/validibot/data`. |
| `MEDIA_ROOT` | Django's media storage. Defaults to a subdirectory of `DATA_STORAGE_ROOT`. |
| `STORAGE_BACKEND` | `local` for self-hosted (default). Future: `s3`, `gcs` for self-hosted with external object storage. |

### 5. Email

| Setting | Purpose |
|---|---|
| `DJANGO_EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` for production. `console` for evaluation. |
| `EMAIL_HOST` | SMTP server. |
| `EMAIL_PORT` | SMTP port (587 for STARTTLS, 465 for SMTPS). |
| `EMAIL_USE_TLS` | `true` for STARTTLS. |
| `EMAIL_USE_SSL` | `true` for SMTPS. |
| `EMAIL_HOST_USER` | SMTP username. |
| `EMAIL_HOST_PASSWORD` | SMTP password. |
| `DEFAULT_FROM_EMAIL` | The `From:` address Validibot uses. |

### 6. Validators

| Setting | Purpose |
|---|---|
| `VALIDATOR_RUNNER` | `docker` for self-hosted (default). `google_cloud_run` for GCP. |
| `VALIDATOR_IMAGE_POLICY` | `tag` (default), `digest`, or `signed-digest` (Phase 5). Production: `digest` or higher. |
| `VALIDATOR_RETAIN_HOURS` | How long stopped validator containers are kept before `cleanup` removes them. Default: `24`. |
| `VALIDATOR_TIMEOUT_SECONDS` | Default per-validator timeout. Validators can request shorter via their manifest. |

### 7. Pro and signing

Empty for community deployments.

| Setting | Purpose |
|---|---|
| `SIGNING_KEY_PATH` | Path to the local ES256 signing key used for signed credentials. Pro only. |
| `JWKS_PUBLIC_PATH` | Path to the public JWKS. Pro only. Served at `/.well-known/jwks.json` for credentials issued by this instance. |
| `IDP_OIDC_MCP_RESOURCE_AUDIENCE` | MCP OAuth audience claim. Defaults to `{VALIDIBOT_MCP_BASE_URL}/mcp`. |
| `VALIDIBOT_MCP_BASE_URL` | Base URL for the MCP server. Defaults to `http://localhost:8001` for self-hosted. |
| `MCP_SERVICE_KEY` | Service-to-service auth key for the MCP server calling the Validibot REST API. Self-hosted only. |
| `MCP_OIDC_AUDIENCE` | Cloud Run OIDC audience (GCP only — self-hosted uses `MCP_SERVICE_KEY`). |
| `ENABLE_MCP_SERVER` | `true` to include the MCP container under the `mcp` Compose profile. Build-time gate; runtime is also gated by the `mcp_server` Pro feature. |

### 8. Optional telemetry

Off by default for self-hosted.

| Setting | Purpose |
|---|---|
| `SENTRY_DSN` | If set, error reporting goes to Sentry. Empty by default. |
| `VALIDIBOT_TELEMETRY` | `off` (default) — future: `errors-only`, `anonymous-usage`, `support-session`. |

## `.postgres` — Postgres settings

| Setting | Purpose |
|---|---|
| `POSTGRES_DB` | Database name. Default: `validibot`. |
| `POSTGRES_USER` | Database user. Default: `validibot`. |
| `POSTGRES_PASSWORD` | Database password. Generate a strong one — this is the credential the web/worker containers use. |
| `POSTGRES_HOST` | Hostname. Default: `postgres` (the Compose service). For external Postgres, set to your DB host. |
| `POSTGRES_PORT` | Port. Default: `5432`. |

## `.build` — image versions and package index

| Setting | Purpose |
|---|---|
| `VALIDIBOT_IMAGE_TAG` | Validibot image tag. `latest` for evaluation, exact version (e.g. `0.8.0`) for production. |
| `VALIDIBOT_IMAGE_REGISTRY` | Image registry. Default: `ghcr.io/validibot`. Mirror: `validibot` on Docker Hub. |
| `VALIDIBOT_COMMERCIAL_PACKAGE` | For Pro: `validibot-pro==<version>`. Empty for community. |
| `VALIDIBOT_PRIVATE_INDEX_URL` | For Pro: `https://<email>:<token>@pypi.validibot.com/simple/`. Empty for community. |

For Pro, the private index URL embeds your license credentials. Treat `.build` as a secret file (mode 0700, not in version control).

## `.mcp` — MCP server settings (Pro feature)

| Setting | Purpose |
|---|---|
| `MCP_PORT` | Port the MCP container listens on. Default: `8001`. |
| `MCP_LOG_LEVEL` | `info` for production. `debug` for troubleshooting. |
| `IDP_OIDC_MCP_SERVER_REDIRECT_URIS` | OIDC redirect URI for the MCP confidential client. Defaults to `[{VALIDIBOT_MCP_BASE_URL}/auth/callback]`. |

## Deployment profiles

A profile is the combination of (target, stage, edition) that controls doctor-check severity, feature gating, and defaults.

| Profile | Purpose | Defaults |
|---|---|---|
| `local-dev` | local contributor work | debug on, local email, no TLS |
| `local-eval` | trial on a laptop/VM | quick start, generated secrets, demo data |
| `self-hosted` | production single VM | debug off, backups, TLS, strict checks |
| `self-hosted-hardened` | risk-averse customer | digest-pinned validators, no telemetry, no runtime license phone-home, local signing/JWKS checks, rootless/socket proxy docs |
| `gcp` | hosted Validibot | GCP/Stripe/metering/x402 |
| `gcp-staging` | pre-prod GCP | same as `gcp` with staging defaults |

Set with `DEPLOYMENT_PROFILE=<profile>` in `.django`. The doctor command uses the profile to decide which checks fail vs warn.

## Settings module switching

Validibot uses Django settings modules to control which apps and features are loaded:

| Module | Use |
|---|---|
| `config.settings.local` | Community-only local dev. Used by `just local`. |
| `config.settings.local_pro` | Community + `validibot_pro` mounted as a volume. Used by `just local-pro`. |
| `config.settings.production` | Self-hosted community production. |
| `config.settings.production_pro` | Self-hosted Pro production — adds `validibot_pro` to `INSTALLED_APPS`. |

Switching from community to Pro is a settings module change in `.django` plus a package install in `.build`. See [Install](install.md) and the [customer-onboarding doc](https://github.com/danielmcquillen/validibot-project/blob/main/docs/operations/customer-onboarding.md) for the full activation flow.

## Reverse proxy: bring your own, or use bundled Caddy

Caddy ships as an opt-in Compose profile, off by default. Most production operators already have a reverse proxy.

To enable Caddy:

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

The Caddyfile lives at `deploy/self-hosted/caddy/Caddyfile` and uses `SITE_URL` to provision Let's Encrypt certificates.

To bring your own proxy: leave the `caddy` profile off, configure your proxy to forward to the `web` container on port 8000, and set `DJANGO_SECURE_PROXY_SSL_HEADER` plus `DJANGO_CSRF_TRUSTED_ORIGINS` appropriately.

## Verifying configuration

```bash
just self-hosted check-env       # parse env files and warn about missing settings
just self-hosted check-dns       # verify SITE_URL resolves to this VM
just self-hosted doctor          # full health check
just self-hosted doctor --json   # machine-readable output for CI
just self-hosted doctor --strict # fail on warnings (suitable for CI gates)
```

Doctor's check IDs are documented in [doctor-check-ids.md](doctor-check-ids.md).

## See also

- [Install](install.md) — initial setup
- [Doctor Check IDs](doctor-check-ids.md) — what each check ID means
- [Security Hardening](security-hardening.md) — recommended hardening
- [Operator Recipes](operator-recipes.md) — full recipe reference

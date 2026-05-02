# Operator Recipes Reference

This is the full reference for `just self-hosted` recipes. Every recipe has an equivalent `just gcp` recipe by design — see the cross-target parity table below.

For the architectural rationale, see the [Operator Capability Matrix](https://github.com/danielmcquillen/validibot-project/blob/main/docs/architecture/operator-capability-matrix.md) (founder-facing).

## Cross-target parity table

Every operator capability exists for both targets:

| Capability | `just self-hosted <cmd>` | `just gcp <cmd>` |
|---|---|---|
| Install / bootstrap | `bootstrap` | `bootstrap` |
| Health diagnostic | `doctor` | `doctor` |
| Smoke test | `smoke-test` | `smoke-test` |
| Backup | `backup` | `backup` |
| Restore | `restore <path>` | `restore <stage> <path>` |
| Upgrade | `upgrade --to <version>` | `deploy <stage> --to <version>` |
| Support bundle | `collect-support-bundle` | `collect-support-bundle` |
| Validator inventory | `validators list-images` | `validators list-images` |
| Cleanup | `cleanup` | `cleanup` |
| Errors since | `errors-since <window>` | `errors-since <stage> <window>` |

GCP recipes take an explicit stage argument (`dev`, `staging`, `prod`). Self-hosted recipes don't — self-hosted is single-stage per VM.

## Recipe details

### `just self-hosted bootstrap`

First-time setup. Creates the env tree, generates secrets where appropriate, validates config, brings up the stack, runs migrations, creates a superuser, registers OIDC clients.

Idempotent — re-runnable after a partial failure. Each step prints `starting / done / skipped (already done)`.

### `just self-hosted deploy`

Deploy or upgrade. Pulls images, runs migrations, restarts services. Used both for first-time deploy after `bootstrap` and for routine updates.

For versioned upgrades, prefer `upgrade --to <version>` — it adds pre-flight checks and a strict upgrade-path check.

### `just self-hosted doctor [--json] [--strict] [--provider <name>]`

Full health diagnostic. Returns structured findings against a stable JSON schema (`validibot.doctor.v1`).

Flags:

- `--json` — machine-readable output, suitable for CI gates and support bundles.
- `--strict` — warn-level findings exit non-zero. Suitable for CI.
- `--provider digitalocean` — adds DigitalOcean-specific checks (DNS, volume mount, monitoring agent, firewall reminder).
- `--post-start` — checks that depend on the stack already being up (e.g. HTTPS certificate validity).
- `--preflight` — pre-deploy checks only.

Check IDs are documented in [doctor-check-ids.md](doctor-check-ids.md).

### `just self-hosted smoke-test`

End-to-end demo workflow. Creates an isolated demo org/user, imports a demo workflow, runs a small built-in validation, runs an advanced validator if Pro is installed, exports an evidence bundle, issues and verifies a signed credential if Pro is installed.

Idempotent. Demo data is clearly marked.

### `just self-hosted status`

Shows which services are running. Wraps `docker compose ps`.

### `just self-hosted logs [service]`

Follows logs from all services, or a specific service if one is named. Wraps `docker compose logs -f`.

### `just self-hosted health-check`

Quick service-health check. Faster than `doctor` and useful in tight loops. Returns non-zero if any service is unhealthy.

### `just self-hosted check-env`

Parses `.envs/.production/.self-hosted/*` and warns about missing or invalid settings. Useful before running `bootstrap` or `deploy`.

### `just self-hosted check-dns`

Verifies that `SITE_URL` resolves to this VM's public IP. Run before enabling Caddy / TLS to prevent certificate confusion.

### `just self-hosted backup [--dry-run]`

Application-level backup. Captures Postgres dump + `DATA_STORAGE_ROOT` archive + env files + manifest with checksums.

Output: `backups/<timestamp>/`.

`--dry-run` shows what would be captured without writing anything.

See [backups.md](backups.md) for the full backup story.

### `just self-hosted restore <path> [--dry-run] [--components <mode>] [--force]`

Restore from a backup directory.

Flags:

- `--dry-run` — shows what would be restored without making changes.
- `--components config-only|data-only|full` — selective restore. Default is `full`.
- `--force` — required to overwrite existing data.

Calls `doctor --strict` first. Refuses to start if doctor reports any `ERROR` or `FATAL`.

See [restore.md](restore.md) for the full restore story.

### `just self-hosted upgrade --to <version> [--no-backup] [--drain]`

Versioned upgrade.

Lifecycle:

1. `doctor --strict` pre-flight (refuses if anything is `ERROR` or `FATAL`).
2. Backup (unless `--no-backup`).
3. Pull version-pinned images.
4. Run migrations in a one-off container.
5. `collectstatic` if needed.
6. Restart services.
7. `doctor --post-upgrade`.
8. `smoke-test`.
9. Write upgrade report.

Flags:

- `--no-backup` — skip the automatic backup. **Not recommended.**
- `--drain` — stop accepting new runs, wait for active runs to finish (default 5 min, configurable), then proceed. Recommended for long-running validators.

Refuses cross-major-version jumps (e.g. v0.8.x → v1.0.0) — the message points at intermediate stops.

Idempotent — re-runnable after a partial failure.

See [upgrades.md](upgrades.md) for the full upgrade story.

### `just self-hosted collect-support-bundle`

Generates a redacted support archive. Includes doctor output, versions, resolved Compose config (with secrets redacted), service status, recent logs, disk usage, migration state, validator manifests.

Excludes secrets, API tokens, signing keys, and raw submission contents.

Output: `support-bundle-<timestamp>.zip` in the current directory.

See [support-bundle.md](support-bundle.md) for the full redaction rules.

### `just self-hosted validators list-images`

Lists installed validator backend images with their tags and digests.

### `just self-hosted validators smoke-test`

Runs each advanced validator with a known-good demo input. Useful after upgrading validator images, after Docker daemon restarts, or when troubleshooting.

### `just self-hosted validators sync-manifests`

Re-synchronises the validator manifests from the installed `validibot-pro` package. Useful after a Pro upgrade if doctor reports `VALIDATOR_DIGEST_MISSING` for a system validator.

### `just self-hosted cleanup [--dry-run]`

Removes:

- stopped validator containers older than `VALIDATOR_RETAIN_HOURS` (default 24h);
- dangling Docker images from previous Compose builds;
- expired backups past retention (default 30d);
- rotated log files past max age.

Always shows a dry-run summary before destructive action, even without `--dry-run`. Operators confirm before deletion.

Safe to run on a cron schedule.

### `just self-hosted errors-since <window>`

Greps the last N units of `docker compose logs` for ERROR / EXCEPTION / TRACEBACK across all services. Five-line shell wrapper.

```bash
just self-hosted errors-since 1h
just self-hosted errors-since 24h
just self-hosted errors-since 7d
```

Pattern adopted from GitLab's `gitlab-ctl tail`.

### `just self-hosted check-updates`

Optional, opt-in. Polls the upstream registry for newer Validibot/validator image versions. Reports without taking action.

Not enabled by default for self-hosted — risk-averse operators reject outbound polling.

### `just self-hosted license-status`

Optional, opt-in. Checks the package-index credential against the Validibot license server. Reports tier and active/expired state.

Not enabled by default. The package-index credential is the entitlement gate; runtime license checks are not required.

## Bash wrapper scripts

Optional wrappers in `deploy/self-hosted/scripts/` for operators who don't have `just` installed:

| Script | Purpose | Notes |
|---|---|---|
| `bootstrap-host` | Install Docker/Compose, create `validibot` user, create directories, set permissions | Generic Linux helper. Run once as root. |
| `bootstrap-digitalocean` | DigitalOcean-tuned wrapper around `bootstrap-host` | Detects Ubuntu LTS, mounted volume, public IP, optional monitoring agent. |
| `check-dns` | Confirm `SITE_URL` resolves to this host before TLS setup | Same as `just self-hosted check-dns`, but runs without `just`. |
| `build-pro-image` | Build a local Pro image from a wheel URL or staged wheel | Uses BuildKit secrets. Avoids leaking private PyPI credentials in image layers. |

These are conveniences, not a parallel toolchain. They wrap the equivalent `just` recipe.

## Same recipes, different stages on GCP

GCP recipes take a stage argument:

```bash
just gcp doctor dev
just gcp doctor staging
just gcp doctor prod
```

Stages map to env file directories under `.envs.example/.production/.google-cloud/<stage>/`. Self-hosted is single-stage, so no stage argument.

The shared interface is what makes the operator experience legible: the same verb means the same thing on both substrates.

## See also

- [Doctor Check IDs](doctor-check-ids.md) — what each check ID means
- [Install](install.md) — initial setup
- [Configuration](configuration.md) — env file reference
- [Backups](backups.md), [Restore](restore.md), [Upgrades](upgrades.md) — major workflows
- [Support Bundle](support-bundle.md) — what's redacted
- [Troubleshooting](troubleshooting.md) — common issues
- [Operator Capability Matrix (founder-facing)](https://github.com/danielmcquillen/validibot-project/blob/main/docs/architecture/operator-capability-matrix.md)

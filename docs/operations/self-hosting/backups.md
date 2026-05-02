# Backups

Validibot ships an application-level backup recipe that captures both the database and the data directory. This page covers what the backup contains, what the manifest looks like, and the rules that govern when a backup is "valid."

This work lands in Phase 3 of the boring-self-hosting ADR.

## What gets backed up

A backup contains two distinct components:

- **Config** — env files at `.envs/.production/.self-hosted/`. These hold secrets and per-deployment configuration. You need these to bring an instance back up.
- **Data** — Postgres dump + `DATA_STORAGE_ROOT` archive. Workflows, runs, evidence, validator resources, uploaded files.

The split is intentional. Operators can do targeted recovery — restore config after a bad env edit without touching data, or restore data after a bad migration without resetting their secrets.

## Backup output (self-hosted)

```text
backups/
  2026-04-27T120000Z/
    manifest.json
    database.sql.zst
    data.tar.zst
    checksums.sha256
```

## Backup manifest schema

Same schema across self-hosted and GCP:

```json
{
  "schema_version": "validibot.backup.v1",
  "validibot_version": "0.8.0",
  "target": "self_hosted",
  "components": {
    "config": {
      "files": [".envs/.production/.self-hosted/*"],
      "checksums": { "...": "..." }
    },
    "data": {
      "database": "database.sql.zst",
      "storage_root": "data.tar.zst",
      "checksums": { "...": "..." }
    }
  },
  "restore_modes_supported": ["config-only", "data-only", "full"]
}
```

GCP variant uses the same schema with `target: "gcp"` and references to the Cloud SQL backup ID and GCS export prefix.

## Commands

```bash
just self-hosted backup           # full backup
just self-hosted backup --dry-run # show what would be captured

just gcp backup prod              # GCP variant; Cloud SQL + GCS export
```

## Backup rules (apply to both self-hosted and GCP)

1. **A backup that has never been restored is not considered valid.** Doctor warns when no restore test has been recorded — see [Restore](restore.md).
2. **Backup commands never silently skip data storage.** If something can't be captured, the command fails with a clear message instead of producing a half-complete archive.
3. **Backup manifests record Validibot version and migration state.** A restore against a different Validibot version is allowed but explicitly logged.
4. **Restore refuses to overwrite existing data unless `--force` is passed.** See [Restore](restore.md) for the full restore policy.
5. **Restore writes a post-restore report and suggests running doctor and smoke-test.**

## Off-host storage

The `backups/` directory lives on the same VM by default. **For production, copy backups off-host.** Options:

- **rsync to a different machine.** Simplest; requires SSH access and a periodic timer.
- **restic.** Encrypted, deduplicated backups to S3-compatible storage, B2, GCS, Azure, SFTP, or local disk. The kit documents `restic` integration as the recommended encrypted backup layer.
- **Cloud provider snapshots.** DigitalOcean automatic Droplet backups, AWS EBS snapshots, etc. Useful for infrastructure-level recovery, but **not a substitute** for application-level backups — they can't reconstruct an evidence-bundle manifest.

For larger customers, document Postgres continuous archiving / PITR (Point-in-Time Recovery) as the recommended upgrade path. Not required for initial pilots.

## Scheduling

The `deploy/self-hosted/systemd/` directory ships with `validibot-backup.timer` + `validibot-backup.service` units. Install them with:

```bash
sudo cp deploy/self-hosted/systemd/validibot-backup.{timer,service} /etc/systemd/system/
sudo systemctl enable --now validibot-backup.timer
```

Default schedule: nightly at 02:00 local. Adjust the timer if your deployment has different load patterns.

For non-systemd hosts, a cron entry works:

```cron
0 2 * * * cd /srv/validibot/repo && just self-hosted backup
```

## What's NOT in a Validibot backup

- container images themselves — pull them from GHCR/Docker Hub on restore;
- the host OS or Docker daemon — that's infrastructure-level concern;
- DNS records or TLS certificates — managed by Caddy/your proxy/your provider;
- application logs older than the rotation window — those go to log rotation, not backup;
- short-lived run workspace dirs (`runs/<org>/<run>/...`) for in-flight runs — those are transient and would be inconsistent in a backup anyway.

## Testing your backup

A backup that has never been restored is not considered valid. Quarterly:

1. Take a backup: `just self-hosted backup`.
2. Spin up a restore environment (a temporary Droplet or local Compose stack).
3. Restore from the backup: `just self-hosted restore backups/<timestamp>` — see [Restore](restore.md).
4. Run `just self-hosted doctor` — should pass.
5. Run `just self-hosted smoke-test` — should pass.
6. Tear down the restore environment.

Doctor records the timestamp of the most recent successful restore drill. The restore-test marker check (VB411) warns if no marker is recorded.

## See also

- [Restore](restore.md) — restore drills, component selection, dry-runs
- [Upgrades](upgrades.md) — backup is required before upgrade
- [Security Hardening](security-hardening.md) — off-host backup recommendation
- [DigitalOcean Provider Guide](providers/digitalocean.md) — application backups vs Droplet backups

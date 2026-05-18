# Backups

Validibot ships an application-level backup recipe that captures the database and the data directory and writes a self-describing manifest. This page covers what a backup contains, the manifest schema, and the rules that govern when a backup is "valid."

This work landed in Phase 3 of the [boring self-hosting ADR](https://github.com/validibot/validibot-project/blob/main/docs/adr/2026-04-27-boring-self-hosting-and-operator-experience.md).

## What gets backed up

A manifested backup contains:

- **Database** — `pg_dump --format=plain` of the running Postgres database, streamed through `zstd` for compression.
- **Data archive** — `tar --zstd` of the contents of `DATA_STORAGE_ROOT`. Workflows, runs, evidence, validator resources, and uploaded files all live there.
- **Manifest** — a `validibot.backup.v1` JSON document recording the Validibot version, full migration head, Postgres server version, file sizes, sha256 checksums, and the exact restore command.
- **Checksums sidecar** — a BSD-style `checksums.sha256` file you can verify with `sha256sum -c` independently of the manifest.

What's NOT in a manifested backup:

- Container images themselves — pull them from GHCR / Docker Hub on restore.
- The host OS or Docker daemon — that's an infrastructure-level concern.
- DNS records or TLS certificates — managed by Caddy, your reverse proxy, or your DNS provider.
- Application logs older than the rotation window — those go to log rotation, not backup.
- Short-lived run workspace dirs (`runs/<org>/<run>/...`) for in-flight runs — those are transient and would be inconsistent in a backup anyway.
- Env files and secret values — see "Config component" below for what the manifest *does* record.

## Backup output

```text
backups/
  20260427T120000Z/
    manifest.json          # validibot.backup.v1 JSON; restore reads this first
    db.sql.zst             # pg_dump --format=plain | zstd
    data.tar.zst           # tar --zstd of DATA_STORAGE_ROOT contents
    checksums.sha256       # sha256sums for the three files above
```

The directory name is a UTC timestamp (`YYYYMMDDTHHMMSSZ`) chosen at backup start. Operators reference this when restoring:

```bash
just self-hosted restore backups/20260427T120000Z
```

## Manifest schema

`validibot.backup.v1` is the cross-target contract — produced by both self-hosted and GCP backups, consumed by both restore tooling paths. Its Pydantic definition lives in `validibot/core/backup_manifest.py`.

```json
{
  "schema_version": "validibot.backup.v1",
  "backup_id": "20260427T120000Z",
  "created_at": "2026-04-27T12:00:01.234567+00:00",
  "target": "self_hosted",
  "stage": null,
  "backup_uri": "file:///srv/validibot/repo/backups/20260427T120000Z/",
  "compatibility": {
    "validibot_version": "0.8.0",
    "python_version": "3.13.1",
    "postgres_server_version": "16.3 (Debian 16.3-1.pgdg120+1)",
    "migration_head": {
      "workflows": "0018_add_workflow_publish_invariants",
      "validations": "0047_alter_validationrun_source_choices"
    }
  },
  "data": {
    "db_dump": {
      "path": "db.sql.zst",
      "size_bytes": 4923847,
      "content_type": "application/zstd",
      "checksum_sha256": "f7a3...64-hex-chars"
    },
    "media_files": [
      {
        "path": "data.tar.zst",
        "size_bytes": 12483920,
        "content_type": "application/zstd",
        "checksum_sha256": "9b1c...64-hex-chars"
      }
    ]
  },
  "config": null,
  "restore_command_hint": "just self-hosted restore backups/20260427T120000Z"
}
```

A few things worth noticing:

- **`schema_version` is a Literal** — `BackupManifest` rejects any value other than `validibot.backup.v1`. A v2 schema appearing in the wild would fail-fast at parse time rather than silently round-trip into a half-understood restore.
- **`compatibility.migration_head`** is the strict gate restore uses to refuse "backup ahead of code" imports. Don't expect to restore a backup taken on a newer Validibot release onto an older one.
- **`config`** is `null` for self-hosted backups today. The cross-target schema reserves space for env-file inventories and Secret Manager versions; the self-hosted MVP doesn't capture them because env files often live outside the repo and operators expect to manage them with their existing config-management tooling.
- **`media_files`** carries a single entry on self-hosted (the data archive). On GCP it carries a per-file inventory of the rsynced media bucket.

## Commands

```bash
just self-hosted backup                       # full manifested backup
just self-hosted list-backups                 # list manifested + db-only backups
just self-hosted backup-db                    # ad-hoc db-only dump (no manifest)

just gcp backup prod                          # GCP variant — Cloud SQL export + GCS rsync
```

`backup` accepts no arguments — self-hosted is single-stage per VM. To direct backups to a different root (e.g. a mounted external volume), set `BACKUP_DIR`:

```bash
BACKUP_DIR=/var/backups/validibot just self-hosted backup
```

The recipe is intentionally narrow:

- It does *not* take a `--dry-run` flag yet. Use `just self-hosted check-env` and `just self-hosted doctor` to verify the deployment is healthy before running.
- It does *not* take a `--components` flag yet. The manifest schema reserves space for separate `config` and `data` restore selection (boring-self-hosting ADR AC #17), but the MVP always captures both halves of what it can capture, and restore consumes them as a unit.

These are documented follow-ups, not gaps in design.

## What backup does, step by step

When you run `just self-hosted backup`, the recipe prints six numbered steps:

1. **Prepare destination.** Create `backups/<id>/`, idempotent so a re-run after a transient failure is safe.
2. **Dump Postgres** via `docker compose exec -T postgres pg_dump --format=plain` piped through host-side `zstd -19`.
3. **Archive `DATA_STORAGE_ROOT`** via `docker compose exec -T web tar --zstd -cf - -C $DATA_ROOT .` streamed to the host's `data.tar.zst`. Container's `tar` shells out to its bundled `zstd`.
4. **Compute checksums.** sha256 + size for each artifact, captured into shell variables.
5. **Write the manifest** by piping a one-line JSONL inventory into `docker compose exec -T web python manage.py write_backup_manifest --media-inventory -` and capturing stdout. The writer runs inside the web container so it sees Django's live migration state.
6. **Sidecar `checksums.sha256`** in BSD-style format. Operators verify any time with `(cd backups/<id> && sha256sum -c checksums.sha256)`.

Each step prints `Step N/6: ...` and `✓ ...` so an operator following along sees what's running.

## Backup rules (apply to both self-hosted and GCP)

1. **A backup that has never been restored is not considered valid.** Doctor warns when no restore-test marker has been recorded — see the [Restore](restore.md) page.
2. **Backup commands never silently skip data storage.** If `pg_dump` or `tar` fails, the recipe exits non-zero and the artifacts left in `backups/<id>/` are clearly partial.
3. **Backup manifests record Validibot version and migration state.** Restore refuses cross-major-version jumps and refuses to import a backup whose migration head is ahead of current code.

## Off-host storage

The `backups/` directory lives on the same VM by default, relative to the repo checkout. On the recommended DigitalOcean layout this is `/srv/validibot/repo/backups/`. **For production, copy backups off-host.** A backup that lives only on the host that produced it is one disk failure away from being no backup at all.

Options:

- **rsync to a different machine.** Simplest; requires SSH access and a periodic timer. A nightly `rsync -a backups/ off-host:/srv/validibot-backups/` job is a reasonable starting point.
- **restic.** Encrypted, deduplicated backups to S3-compatible storage, B2, GCS, Azure, SFTP, or local disk. The backup recipe writes to the local filesystem; pointing a `restic backup` at `backups/<id>/` after every run gives you encrypted off-host storage with retention.
- **Cloud provider snapshots.** Useful for *infrastructure*-level recovery, but **not a substitute** for application-level backups. DigitalOcean automatic Droplet backups do not include attached Volumes, and DigitalOcean Volume snapshots are crash-consistent rather than application-aware. Snapshots can't reconstruct a `manifest.json` and they don't pass through `verify_backup_compatibility` on restore.

For larger customers, document Postgres continuous archiving / PITR (Point-in-Time Recovery) as the recommended upgrade path. Not required for initial pilots.

## Scheduling

A simple cron entry runs nightly:

```cron
0 2 * * * cd /srv/validibot/repo && just self-hosted backup >> /var/log/validibot-backup.log 2>&1
```

For systemd hosts, future work in `deploy/self-hosted/systemd/` will ship `validibot-backup.timer` + `validibot-backup.service` units. Until those land, cron is the canonical scheduling path.

## Verifying a backup without restoring

`checksums.sha256` is BSD-style sha256, the format `sha256sum -c` consumes:

```bash
cd backups/20260427T120000Z
sha256sum -c checksums.sha256
# manifest.json: OK
# db.sql.zst:    OK
# data.tar.zst:  OK
```

For a deeper check that exercises the manifest's compatibility gates without touching the live deployment, run the verifier as a one-off:

```bash
cat backups/20260427T120000Z/manifest.json | \
  docker compose -f docker-compose.production.yml -p validibot exec -T web \
  python manage.py verify_backup_compatibility --manifest -
```

The verifier exits 0 (`COMPATIBLE`) or 64 (`REFUSED`) and prints which compatibility class failed (schema version, migration head, version jump). This is the exact same check restore runs as its third pre-flight gate.

## Testing your backup

A backup that has never been restored is not considered valid. Quarterly:

1. Take a backup: `just self-hosted backup`.
2. Spin up a restore environment (a temporary Droplet or local Compose stack on a different host).
3. Copy `backups/<id>/` to that environment.
4. Restore: `just self-hosted restore backups/<id>`. The recipe writes `.last-restore-test` in `DATA_STORAGE_ROOT` on success.
5. Run `just self-hosted doctor` — VB411 should now report OK.
6. Run `just self-hosted smoke-test` — end-to-end validation should work.
7. Tear down the restore environment.

Doctor's `VB411` reads the marker's mtime to compute restore-test staleness. >90 days produces a `WARN`. See [Restore](restore.md) for the full restore-drill walkthrough.

## See also

- [Restore](restore.md) — the four pre-flight gates, drill walkthrough, recovery scenarios
- [Upgrades](upgrades.md) — backup is required before upgrade; rollback is restore-from-backup
- [Security Hardening](security-hardening.md) — off-host backup recommendation, encryption-at-rest
- [DigitalOcean Provider Guide](providers/digitalocean.md) — application backups vs Droplet backups
- [Doctor Check IDs](doctor-check-ids.md) — VB411 restore-test marker, VB101-VB199 database checks

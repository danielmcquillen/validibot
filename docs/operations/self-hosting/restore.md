# Restore

A backup that has never been restored is not considered valid. This page covers the restore command, component selection, dry-runs, and the doctor-marker that records when a restore drill last passed.

This work lands in Phase 3 of the boring-self-hosting ADR.

## The principle

> Restore is more important than backup.

Backups create confidence only if restore has been tested. Doctor makes untested restores visible. Phase 3 of the boring-self-hosting ADR turns this from "good practice" into a measurable thing: VB411 (restore-test marker) is a `WARN` finding by default, and the operator-readiness checklist (Phase 6) requires running a restore drill before declaring a pilot install complete.

## Commands

```bash
just self-hosted restore backups/2026-04-27T120000Z
just self-hosted restore backups/2026-04-27T120000Z --dry-run
just self-hosted restore backups/2026-04-27T120000Z --components config-only
just self-hosted restore backups/2026-04-27T120000Z --components data-only
just self-hosted restore backups/2026-04-27T120000Z --components full   # default

just gcp restore prod backups/2026-04-27T120000Z
```

### Component selection

The backup manifest separates `config` and `data` components. Restore lets you select either or both:

| Mode | Restores | When to use |
|---|---|---|
| `config-only` | `.envs/.production/.self-hosted/*` env files | After a bad env edit; data is fine |
| `data-only` | Postgres + `DATA_STORAGE_ROOT` | After a bad migration; secrets unchanged |
| `full` (default) | Both | Disaster recovery; spinning up on a fresh host |

Pattern adopted from GitLab's split between `gitlab-ctl backup-etc` (config + secrets) and `gitlab-backup create` (data).

## Restore policy

1. **Restore refuses to overwrite existing data unless `--force` is passed.** A clean target host is the safe default.
2. **Pre-flight check.** `restore` calls `doctor --strict` first and refuses to proceed if anything is `ERROR` or `FATAL`. "Refuse early, refuse loudly" beats "fail half-way through with corrupt state."
3. **Idempotent retry.** Each restore step prints "starting / done / skipped (already done)." After a partial failure, re-running the recipe picks up where it left off.
4. **Post-restore report.** Restore writes a report to `backups/restored/<timestamp>/report.json` and prints suggestions to run doctor and smoke-test.
5. **Restore-test marker.** A successful restore in a non-production-target context (a temp host, a fresh local stack, a CI job) records a marker that doctor reads. Restoring into production for actual recovery does **not** advance the marker.

## Dry-run

`--dry-run` prints what would happen without making changes:

```bash
just self-hosted restore backups/2026-04-27T120000Z --dry-run
```

Output includes:

- which files would be written;
- which database tables would be replaced;
- estimated bytes;
- doctor's verdict on the target's current state;
- whether `--force` would be required.

A green dry-run is a strong signal that the restore will succeed. It is **not** a substitute for an actual restore drill.

## The quarterly drill

Quarterly:

1. Take a backup: `just self-hosted backup`.
2. Spin up a restore environment (a temporary Droplet, a local Compose stack, or a CI job).
3. Restore: `just self-hosted restore backups/<timestamp>`.
4. Run `just self-hosted doctor` — should pass on the restored target.
5. Run `just self-hosted smoke-test` — end-to-end validation should work.
6. Record the marker (the restore command does this automatically on success).
7. Tear down the restore environment.

This is the difference between "we have backups" and "we have provably working backups." For a customer paying 6k–24k/year, the restore drill is the artefact that justifies the pricing.

## Restore vs DigitalOcean Droplet backups (and similar)

A DigitalOcean snapshot or automatic Droplet backup is useful for infrastructure-level recovery — if the VM is corrupted, you can roll the entire VM back. But these snapshots **do not replace** Validibot's application-level backup/restore, because:

1. they don't produce a manifest that records Validibot version, migration state, or evidence-bundle checksums;
2. they're tied to a specific provider and a specific VM ID;
3. they don't separate config from data, so component-selection restore isn't available;
4. they don't pass through the doctor pre-flight check.

Use both: provider snapshots for "the VM is on fire," Validibot application backups for "the data is corrupted but the VM is fine," "the migration broke," "we need to spin up a new host."

## Restoring on a different host

Common case: a pilot customer's first VM was undersized; they spin up a bigger VM and want to migrate.

1. On the source host: `just self-hosted backup`.
2. Copy `backups/<timestamp>/` to the new host.
3. On the new host, complete the install steps up to before bringing the stack up.
4. Run `just self-hosted restore backups/<timestamp> --components full`.
5. Run `just self-hosted doctor` and `just self-hosted smoke-test`.
6. Update DNS to point at the new host.

The backup manifest's `validibot_version` is checked against the host's running version. A restore against a different version is allowed but explicitly logged. Practice: always restore against the same Validibot version as the backup, then upgrade after.

## Recovering after a bad env edit

```bash
just self-hosted restore backups/<timestamp> --components config-only
just self-hosted doctor
```

This restores the env files from the backup without touching the database or `DATA_STORAGE_ROOT`. Useful when an operator has misedited a setting and wants to roll back without losing recent runs.

## Recovering after a bad migration

```bash
# 1. Stop the stack
just self-hosted stop

# 2. Restore data only
just self-hosted restore backups/<timestamp> --components data-only

# 3. Bring up the previous version's image (env files still pin the new version, so this is a temporary edit)
$EDITOR .envs/.production/.self-hosted/.build  # set VALIDIBOT_IMAGE_TAG to the previous version
just self-hosted deploy

# 4. Verify and plan the proper fix
just self-hosted doctor
just self-hosted smoke-test
```

Rollback across migrations is **restore-from-backup, not reverse migrations**. Validibot does not promise reversible migrations. If you need a true rollback path for a destructive migration, take the backup before the upgrade, and restore if the upgrade goes wrong.

## See also

- [Backups](backups.md) — what gets captured
- [Upgrades](upgrades.md) — backup is required before upgrade; rollback is restore-from-backup
- [DigitalOcean Provider Guide](providers/digitalocean.md) — restore drill on DigitalOcean
- [Doctor Check IDs](doctor-check-ids.md) — VB411 restore-test marker

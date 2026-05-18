# Restore

A backup that has never been restored is not considered valid. This page covers the restore command, the four pre-flight gates that protect against accidents, the doctor marker that records restore-test recency, and the common recovery scenarios.

This work landed in Phase 3 of the [boring self-hosting ADR](https://github.com/validibot/validibot-project/blob/main/docs/adr/2026-04-27-boring-self-hosting-and-operator-experience.md).

## The principle

> Restore is more important than backup.

Backups create confidence only if restore has been tested. Doctor makes untested restores visible: `VB411` (restore-test marker) is a `WARN` finding by default, and the operator-readiness checklist (Phase 6) requires running a restore drill before declaring a pilot install complete. The operational intent is to make "we have backups" indistinguishable from "we have provably working backups."

## Commands

```bash
just self-hosted restore backups/20260427T120000Z      # restore from a manifested backup
just self-hosted list-backups                          # show what's available

just gcp restore prod gs://my-project-validibot-backups-prod/20260427T120000Z
```

Self-hosted is single-stage per VM; the recipe takes a single backup-directory argument and no stage. The argument can be relative or absolute — both work.

## What restore does, step by step

The recipe mirrors the GCP restore flow exactly: four pre-flight gates, an operator confirmation, then five destructive steps. Every gate prints what it's checking; if any gate fails, no destructive operation runs.

### Pre-flight gates

```text
Pre-flight 1/4: manifest located at backups/20260427T120000Z/manifest.json
Pre-flight 2/4: running doctor --strict...
  ✓ doctor --strict passed.

Pre-flight 3/4: verifying backup compatibility...
  COMPATIBLE: backup 20260427T120000Z restorable on deployment.
  ✓ Backup is compatible with this deployment.

Pre-flight 4/4: verifying DB dump integrity...
  ✓ DB dump matches manifest (4923847 bytes, sha256 f7a3c4d8e2b1a09f...).
```

| # | Gate | Failure means |
|---|---|---|
| 1 | Manifest exists at `<path>/manifest.json` | Wrong path or backup is incomplete |
| 2 | `doctor --strict` exits 0 | Deployment is unhealthy; resolve before restoring |
| 3 | `verify_backup_compatibility` exits 0 | Schema mismatch, migration head ahead, or cross-major version jump |
| 4 | DB dump size + sha256 match the manifest | Tampering, truncation, or partial download |

These are sequenced cheap → expensive. Gate 1 is a single `[ -f ... ]`; gate 4 streams the whole DB dump through `sha256sum`. Stopping early on a typo'd path saves an unnecessary doctor run.

### Operator confirmation

Restore is destructive. The recipe prints a clear summary and asks the operator to type the short hostname:

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ⚠ DESTRUCTIVE OPERATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  About to:
    1. DROP and recreate the public schema in validibot
    2. Stream backups/20260427T120000Z/db.sql.zst through psql
    3. Apply any migrations newer than the backup
    4. WIPE DATA_STORAGE_ROOT and extract data.tar.zst

  Type the short hostname 'validibot-prod' to confirm, anything else aborts:
```

An accidental `just self-hosted restore <wrong-path>` typed by mistake will sit at this prompt rather than nuking data.

### Destructive steps

After confirmation:

1. **Reset the public schema** — `DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;`. Standard idiom for "wipe everything before importing a plain-SQL dump." Without this step the incoming `CREATE TABLE` statements would conflict with existing tables.
2. **Stream the dump through psql** — `zstd -dc db.sql.zst | psql -v ON_ERROR_STOP=1`. The `ON_ERROR_STOP` flag makes psql exit on the first error rather than silently leaving a half-imported database.
3. **Apply newer migrations** — `python manage.py migrate --noinput`. Verify-compatibility refused if the backup's migration head is ahead of code; if code is ahead of the backup (the normal case during an upgrade), `migrate` brings the schema forward without touching data.
4. **Wipe and extract data archive** — `find $DATA_ROOT -mindepth 1 -delete` followed by `tar --zstd -xf data.tar.zst -C $DATA_ROOT`. The `-mindepth 1` keeps the directory itself in place (the container expects it).
5. **Touch the restore-test marker** — `touch $DATA_ROOT/.last-restore-test`. Doctor's VB411 reads this file's mtime; this step turns the warning green for the next 90 days.

After step 5 the recipe prints recommended verification:

```bash
just self-hosted doctor
just self-hosted errors-since 5m
```

## Restore policy

1. **Pre-flight gates run unconditionally.** There is no `--force` flag yet. Address the gate's failure first; the recipe protects against half-done states.
2. **Restore writes the `.last-restore-test` marker on success.** Doctor's `VB411` reads it. Re-running restore advances the mtime even if the backup contained an older marker (the tar archive may carry one from a previous restore).
3. **Restore applies migrations on top of the imported state.** This is what makes "restore older backup onto current code" work cleanly — the dump captures schema-as-of-backup, migrations bring it forward, no special handling required.

## ADR scope: what's deferred from the original acceptance criteria

The boring self-hosting ADR's Phase 3 acceptance criteria list three restore surfaces — `--dry-run`, `--components`, `--force` — that are NOT in the MVP. They're documented here so the ADR's status is honest:

| Surface | ADR AC | MVP status | Why deferred |
|---|---|---|---|
| `--dry-run` (preview without applying) | Phase 3 task 8 | **Not implemented** | The four pre-flight gates already make most "would this work?" questions answerable without touching the deployment. Operators can run `verify_backup_compatibility` standalone (see [Backups → Verifying a backup](backups.md#verifying-a-backup-without-restoring)) for the closest equivalent. A real `--dry-run` would simulate the SQL import + tar-extract steps; that adds complexity disproportionate to its value at MVP. |
| `--components config-only` / `data-only` / `full` | Phase 3 task 9 + AC #17 | **Not implemented** | The manifest schema (`validibot.backup.v1`) reserves the slot — `data` and `config` are separate components. The MVP always applies both. The recipe-side branching is a focused additive change when an operator pilot needs it. |
| `--force` (skip pre-flight gates) | Phase 3 task 5 | **Not implemented** | The gates are deliberately unconditional. A `--force` escape hatch would invite operators to bypass the protections under pressure — exactly when they shouldn't. The "right" override path is to fix the doctor finding, not skip it. |

These deferrals are tracked in the ADR follow-up list; if a paid pilot needs any of them, that's a focused implementation rather than a redesign.

## The quarterly drill

Quarterly:

1. Take a backup: `just self-hosted backup`.
2. Spin up a restore environment — a temporary Droplet, a local Compose stack on a different host, or a CI job. Restoring on the same host you're trying to verify defeats the purpose.
3. Copy `backups/<id>/` to that environment.
4. Restore: `just self-hosted restore backups/<id>`. Type the test environment's short hostname at the confirmation prompt.
5. Run `just self-hosted doctor` — VB411 should now report OK; everything else should pass.
6. Run `just self-hosted smoke-test` — end-to-end validation should work.
7. Tear down the restore environment.

This is the difference between "we have backups" and "we have provably working backups." For a customer paying 6k–24k/year, the restore drill is the artefact that justifies the pricing.

## Restore vs DigitalOcean Droplet backups (and similar)

A DigitalOcean snapshot or automatic Droplet backup is useful for infrastructure-level recovery — if the VM is corrupted, you can roll infrastructure back. But these snapshots **do not replace** Validibot's application-level backup/restore, because:

1. They don't produce a manifest that records Validibot version, migration state, or evidence-bundle checksums.
2. They're tied to a specific provider and a specific VM ID, which complicates "spin up a new host on a different provider."
3. They don't pass through `verify_backup_compatibility` — restoring an old snapshot onto current code can resurrect schemas the ORM no longer knows about.
4. They include the host OS, Docker daemon, and system-level state, much of which is not what you actually want during data recovery.
5. On DigitalOcean specifically, automatic Droplet backups do not include attached Volumes. In the recommended layout, Docker named volumes and application backups live on the `/srv/validibot` Volume, so Droplet backups alone are incomplete.

Use both: provider snapshots for "the infrastructure is broken," Validibot application backups for "the data is corrupted but the VM is fine," "the migration broke," "we need to spin up on a new provider." If you take a DigitalOcean Volume snapshot, first run `just self-hosted backup`, stop the stack with `just self-hosted down`, run `sync`, take the snapshot, then bring the stack back up.

## Restoring on a different host

Common case: a pilot customer's first VM was undersized and they're migrating to a bigger one.

1. On the source host: `just self-hosted backup`.
2. Copy `backups/<id>/` to the new host (rsync, scp, restic restore, etc.).
3. On the new host, complete the install steps up to and including `just self-hosted bootstrap`.
4. Run `just self-hosted restore backups/<id>`. Type the new host's short hostname at the confirmation prompt.
5. Run `just self-hosted doctor` and `just self-hosted smoke-test`.
6. Update DNS to point at the new host.

Per the third pre-flight gate, the manifest's `validibot_version` is checked. A restore on the same Validibot version is the easy path; a restore onto a *newer* Validibot version is fine because `migrate` brings the schema forward; a restore onto an *older* Validibot version is refused (cross-major) or implicitly refused (migration head ahead of code).

Practice: always restore against the same Validibot version as the backup, then upgrade after.

## Recovering after a bad migration

```bash
# 1. Stop the stack so no writes are in flight.
just self-hosted down

# 2. Restore from the pre-upgrade backup. Re-runs migrate as part of restore;
#    if you also need to roll the IMAGE TAG back, edit the build env first.
just self-hosted restore backups/<pre-upgrade-id>

# 3. Verify and plan the proper fix.
just self-hosted doctor
just self-hosted smoke-test
```

Rollback across migrations is **restore-from-backup, not reverse migrations**. Validibot does not promise reversible migrations. If you need a true rollback path for a destructive migration, take the backup before the upgrade, and restore if the upgrade goes wrong. The [Upgrades](upgrades.md) page enforces this with a pre-upgrade backup step.

## Recovering after a bad env edit

The MVP does not have `--components config-only` yet, so config-only recovery is a manual restore from your config-management tooling (Ansible, hand-edited git history, etc.) rather than a Validibot recipe. Until the component-selective restore lands, keep the env files under version control or other separate backup.

The reason this isn't a regression: `validibot.backup.v1` reserves a `config` component for exactly this case (see `backup_manifest.py`). It's an additive feature, not a redesign.

## See also

- [Backups](backups.md) — what gets captured, manifest schema, off-host storage
- [Upgrades](upgrades.md) — backup is required before upgrade; rollback is restore-from-backup
- [DigitalOcean Provider Guide](providers/digitalocean.md) — restore drill on DigitalOcean
- [Doctor Check IDs](doctor-check-ids.md) — VB411 restore-test marker

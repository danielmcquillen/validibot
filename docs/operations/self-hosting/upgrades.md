# Upgrades

Validibot's upgrade flow is boring and repeatable. This page covers the full lifecycle, the four pre-flight gates, the strict upgrade-path enforcement, and what happens to in-flight validation runs during an upgrade.

This work landed in Phase 4 of the [boring self-hosting ADR](https://github.com/validibot/validibot-project/blob/main/docs/adr/2026-04-27-boring-self-hosting-and-operator-experience.md).

## The standard flow

```bash
just self-hosted upgrade --to v0.9.0
```

That's it. The recipe handles the entire lifecycle — backup, version-pin, build, migrate, restart, verify — with each step printing what it's doing. If anything fails, the upgrade refuses to leave the deployment in a half-applied state.

The full upgrade lifecycle:

1. **Pre-flight 1/4: doctor --strict** — refuse if the deployment is unhealthy
2. **Pre-flight 2/4: clean working tree** — refuse if there are uncommitted local edits
3. **Pre-flight 3/4: target tag exists** — refuse to upgrade to a version that isn't a real release tag
4. **Pre-flight 4/4: validate upgrade path** — refuse cross-major-version jumps
5. **Step 1/7: Backup** — manifested backup; the rollback insurance policy
6. **Step 2/7: Git checkout target** — switch the working tree to the version tag
7. **Step 3/7: Build images** — `docker compose build`
8. **Step 4/7: Run migrations** — Django `migrate --noinput` in a one-off container
9. **Step 5/7: Restart services** — `docker compose up -d`
10. **Step 6/7: Post-flight doctor** — verify the upgrade landed cleanly
11. **Step 7/7: Post-flight smoke-test** — end-to-end validation through the new code
12. **Write upgrade report** — `backups/upgrades/<target>/report.json`

If any pre-flight gate refuses, no destructive work happens. If a step fails partway through, re-running picks up where it left off (every step is idempotent).

## Pre-flight gates: refuse early, refuse loudly

Pattern adopted from GitLab and Sentry: destructive operations that start from a known-broken state corrupt things further. Better to refuse early with a clear error than fail mid-migration.

### Gate 1 — `doctor --strict`

Calls `just self-hosted doctor --strict` first. If anything is `ERROR`, `FATAL`, or `WARN`, the upgrade refuses to start. The error message names the failing check ID and the suggested fix:

```text
[ERROR] VB201 DATA_STORAGE_ROOT is writable by root only
        Fix: chown -R 1000:1000 /srv/validibot/data
Error: doctor --strict failed. Refusing to upgrade against
       an unhealthy deployment.
```

### Gate 2 — clean working tree

Refuses to proceed if there are uncommitted local edits. The next step is a `git checkout` that would silently lose those edits.

```text
Error: working tree has uncommitted changes.
  Commit or stash them before upgrading:
    git status
    git stash
```

### Gate 3 — target tag exists

Runs `git fetch --tags` and verifies the target tag is reachable. An invalid tag here is operator-friendly to catch *before* the backup runs (which is the slowest pre-flight step).

```text
Error: tag 'v9.9.9' not found in this repository.
  Available recent tags:
    v0.4.0
    v0.3.2
    v0.3.1
  If your release is newer, pull from origin:
    git fetch --tags origin
```

### Gate 4 — validate upgrade path

Cross-major-version jumps are refused. Skipping a major silently skips its migrations and the contractual breaking-change gates that come with a major bump.

```text
Error: cannot upgrade directly from v0.8.0 (major v0) to v1.0.0 (major v1).
  Required intermediate stop: v0.9.0.
  Run:  just self-hosted upgrade --to v0.9.0  first,
  then retry the upgrade to v1.0.0.
```

The "intermediate stop" is computed from the available tags in the repo: it's the latest patch in the current major series. Pattern informed by GitLab's mandatory upgrade stops.

## Idempotent retry

Every step in the upgrade is naturally idempotent:

| Step | Why it's idempotent |
|---|---|
| Backup | Skipped on retry only if `--no-backup` (re-runs create a new backup, harmless) |
| Git checkout | No-op if HEAD is already at the target |
| Docker build | Layer cache makes re-runs essentially free |
| Migrate | Django's `migrate --noinput` is naturally idempotent |
| Restart | `docker compose up -d` no-ops on services with unchanged images |
| Doctor / smoke-test | Read-only |
| Report write | Overwritten on success |

So after a partial failure, you fix the underlying issue and re-run the same command:

```bash
just self-hosted upgrade --to v0.9.0
# ... fails partway through migration step due to network blip ...

# Fix the network issue, then:
just self-hosted upgrade --to v0.9.0
# ... resumes; completed steps no-op; the failed step retries ...
```

After a successful upgrade, re-running with the same target is a no-op:

```text
$ just self-hosted upgrade --to v0.9.0
Upgrade to v0.9.0 already complete — report at backups/upgrades/v0.9.0/report.json.
Re-run `just self-hosted doctor` and `just self-hosted smoke-test`
if you want a fresh health check.
```

This pattern is from Sentry's `install.sh` resume behaviour. The first upgrade is when transient errors are most likely (network, package mirror, DNS) — operators must be able to fix the underlying issue and re-run safely.

## The `--no-backup` escape hatch

`--no-backup` skips the manifested backup step. Use this only when you have an out-of-band recovery story (off-host snapshots, an upstream wal-e archive, etc.). The recipe warns loudly:

```text
Step 1/7: Backup SKIPPED (--no-backup).
          ⚠ Restore from backup is not available if this upgrade fails.
```

For pilot installs and production deployments, take the backup. The default is mandatory backup for a reason.

## Downtime expectations

- **Self-hosted single-VM**: expect 30–60 seconds of HTTP unavailability while the web container restarts. Plus the migration window. Most migrations are quick; release notes flag long-running ones with estimated runtime.
- **GCP Cloud Run**: traffic shifts between revisions, so HTTP stays up except during the migration job. The migration job is the only true downtime window.

The dashboard returns connection errors during the blip. CLI and MCP clients see a reset and should retry.

If you need zero downtime on self-hosted, deploy on GCP — single-VM doesn't promise zero-downtime upgrades across breaking schema changes. That's the cost of "boring."

## Queued and in-flight validation runs

- **Queued runs survive upgrades.** The Redis broker persists them; new workers pick them up after restart.
- **Running validations** use `acks_late=True` with a graceful shutdown timeout. A worker killed mid-task either finishes or its task is re-delivered to a new worker on the upgraded image. Tasks must be written to be idempotent under re-delivery.
- **Destructive migrations** are genuinely incompatible with old workers. A breaking schema change plus an old worker against the new schema will crash the worker. This is the cost of "boring" — we don't promise zero-downtime upgrades across breaking schema changes.

For deployments running long advanced validators (EnergyPlus, FMU runs that take minutes), schedule upgrades during a maintenance window. Future work may add a `--drain` flag that waits for active runs to finish before proceeding.

## Rollback policy

Rollback across migrations is **restore-from-backup, not reverse migrations**. Validibot does not promise reversible migrations.

If an upgrade fails the post-flight checks (doctor or smoke-test reports issues that the upgrade caused), the recipe prints the backup path:

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ⚠ Upgrade to v0.9.0 landed with post-flight findings
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Report:  backups/upgrades/v0.9.0/report.json
  Backup:  backups/20260505T143022Z/

  Recommended next steps:
    just self-hosted doctor                    # re-run to see findings
    just self-hosted errors-since 5m           # recent service errors
    just self-hosted restore backups/20260505T143022Z/  # roll back
```

If you decide to roll back: stop the stack, restore the backup, git-checkout the previous tag, restart.

```bash
just self-hosted down
just self-hosted restore backups/20260505T143022Z/
git checkout v0.8.0   # the previous tag
just self-hosted up
just self-hosted doctor
```

## Upgrade report (`validibot.upgrade.v1`)

Every successful upgrade writes `backups/upgrades/<target>/report.json` so the operator and any future audit have a record of what happened:

```json
{
  "schema_version": "validibot.upgrade.v1",
  "host": "validibot-prod",
  "from_version": "v0.8.0",
  "to_version": "v0.9.0",
  "started_at": "2026-05-05T14:30:22Z",
  "completed_at": "2026-05-05T14:34:18Z",
  "backup_path": "backups/20260505T143022Z",
  "backup_skipped": false,
  "doctor_pass": true,
  "smoke_test_pass": true
}
```

The schema is operator-facing and pinned. Additive fields stay v1; renaming or removing requires v2.

## Version policy

- **Pin to an exact version tag.** `just self-hosted upgrade --to v0.9.0`. Don't use moving tags like `v0.9` or `latest` for production.
- **Validator backend images are versioned separately.** They're recorded in evidence bundles and tracked by their digest. See [validator-images.md](validator-images.md).
- **Pre-release tags work.** `--to v0.9.0-rc1` is accepted; the recipe regex matches `vX.Y.Z[-suffix]`.
- **Support asks for `doctor --json` and the upgrade report first.**

## GCP — same lifecycle, different verb

```bash
just gcp upgrade prod v0.9.0
```

GCP runs the same four pre-flight gates and the same backup → checkout → deploy → verify lifecycle. The only differences:

| Step | Self-hosted | GCP |
|---|---|---|
| Backup | `just self-hosted backup` (Postgres dump + tar) | `just gcp backup <stage>` (Cloud SQL export + GCS rsync) |
| Build + deploy | `docker compose build` + `migrate` + `up -d` | `just gcp deploy-all <stage>` (image push + Cloud Run Job migrate + traffic shift) |
| Doctor / smoke-test | Local container | Cloud Run Jobs |

We use `upgrade` on both targets (rather than `deploy --to` on GCP) because the gated lifecycle is the same operator concern in both worlds. The existing `just gcp deploy <stage>` stays as the team's daily fast-path; `upgrade` is the explicit version-transition with every safety rail engaged.

## Release notes contract

Every release should satisfy the [release-notes policy](release-notes-policy.md):

- **⚠ Required operator action** — checkable list
- **Breaking changes** — explicit, even if "none"
- **Database migrations** — listed with estimated runtime and reversibility
- **Validator image changes** — version bumps with breaking-change notes
- **Manual operator action** — anything that doesn't auto-apply

Operators rely on release notes *before* upgrading; if breaking changes are buried, they become production incidents.

## Things that aren't built yet

The Phase 4 MVP doesn't yet implement:

- **`--drain` flag** — wait for in-flight runs to finish before proceeding. Recommended pattern for deployments running long validators; until it lands, schedule upgrades manually during quiet windows.
- **`--components`-aware rollback** — restore is whole-system today. Component-selective restore is reserved in `validibot.backup.v1` (the manifest schema has the slot) but not yet wired through the recipe.
- **Pre-built image pulls** — the recipe checks out the version tag and builds locally. A future enhancement could pull a pre-built image from GHCR / Docker Hub when the network can reach them, falling back to local build otherwise.

These are documented follow-ups, not gaps in design.

## See also

- [Backups](backups.md) — pre-upgrade backup is the rollback insurance
- [Restore](restore.md) — rollback path
- [Release Notes Policy](release-notes-policy.md) — what every release announces
- [Doctor Check IDs](doctor-check-ids.md) — what pre-flight checks
- [Smoke test](smoke-test.md) — end-to-end post-flight verification
- [Operator Recipes](operator-recipes.md) — full recipe reference

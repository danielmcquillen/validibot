# Upgrades

Validibot's upgrade flow is boring and repeatable. This page covers the lifecycle, the pre-flight checks, the strict upgrade-path enforcement, and what happens to in-flight validation runs during an upgrade.

This work lands in Phase 4 of the boring-self-hosting ADR.

## The standard flow

```bash
just self-hosted backup
just self-hosted upgrade --to v0.9.0
just self-hosted doctor --post-upgrade
just self-hosted smoke-test
```

That's the entire flow. Backup → upgrade → verify → smoke-test. The `upgrade` recipe handles every step in between.

## Lifecycle

Upgrade does these steps in order:

1. **Pre-flight check** — runs `doctor --strict` and refuses to proceed if anything is `ERROR` or `FATAL`.
2. **Backup** — automatic unless `--no-backup` is explicitly passed. The backup path is printed to stderr before any other change.
3. **Image pull** — pulls the requested version-pinned images from GHCR (or Docker Hub mirror).
4. **Migrations** — runs in a one-off container so the migration job can fail without affecting the running stack.
5. **`collectstatic`** — runs if needed.
6. **Service restart** — web and worker containers get the new image. Postgres, Redis, and validator containers don't restart unless explicitly required by the release notes.
7. **Doctor (post)** — runs `doctor` again on the new revision.
8. **Smoke test** — runs the demo workflow end-to-end.
9. **Upgrade report** — written to `backups/upgrades/<timestamp>/report.json`.

## Pre-flight check: refuse early

Upgrade calls `doctor --strict` first. If anything is `ERROR` or `FATAL`, the upgrade refuses to start. The error message names the failing check ID and the suggested fix.

> ERROR: VB201 DATA_STORAGE_ROOT is writable by root only
> Fix: chown -R 1000:1000 /srv/validibot/data
> Refusing to upgrade with ERROR-level findings. Re-run after fixing.

Pattern adopted from GitLab and Sentry — destructive operations that start from a known-broken state corrupt things further. Better to refuse early with a clear error than fail mid-migration.

## Strict upgrade-path enforcement

Cross-major-version jumps are refused. Skipping major versions silently skips migrations.

```text
$ just self-hosted upgrade --to v1.0.0
ERROR: Cannot upgrade directly from v0.8.x to v1.0.0.
       Required intermediate stop: v0.9.0.
       Run: just self-hosted upgrade --to v0.9.0 first, then retry.
```

Pattern informed by GitLab's mandatory upgrade stops. Required intermediate stops are documented in the release notes; see [release-notes-policy.md](release-notes-policy.md).

## Idempotent retry

Each upgrade step prints `starting / done / skipped (already done)`. After a partial failure, re-running `upgrade --to <version>` picks up where it left off rather than redoing everything (which would re-trigger migrations, etc.).

```bash
just self-hosted upgrade --to v0.9.0
# ... fails partway through migration step due to network blip ...

# Fix the network issue, then:
just self-hosted upgrade --to v0.9.0
# ... resumes at the migration step; doesn't re-pull the image, doesn't re-run completed steps ...
```

Pattern adopted from Sentry's `install.sh` resume behaviour. This matters because the first upgrade is also when transient errors (network, package mirror, DNS) are most likely — operators must be able to fix the underlying issue and re-run safely.

## Downtime expectations

- expect **30–60 seconds of HTTP unavailability** while the web container restarts;
- plus the **migration window** in step 4. Most migrations are quick; release notes flag long-running ones with estimated runtime.

The dashboard returns connection errors during the blip. CLI and MCP clients see a reset and should retry.

If you need zero downtime, deploy on GCP — Cloud Run shifts traffic between revisions, so HTTP stays up. Self-hosted single-VM does not promise zero-downtime upgrades across breaking schema changes; that's the cost of "boring."

## Queued and in-flight validation runs

- **queued runs** survive upgrades. The broker persists them; new workers pick them up after restart.
- **running validations** use `acks_late=True` with a graceful shutdown timeout. A worker killed mid-task either finishes or its task is re-delivered to a new worker on the upgraded image. Tasks must be written to be idempotent under re-delivery.
- **destructive migrations** are genuinely incompatible with old workers. A breaking schema change plus an old worker against the new schema will crash the worker. This is the cost of "boring" — we don't promise zero-downtime upgrades across breaking schema changes.

### `--drain` for long-running validators

If you run validators that take 30+ seconds (EnergyPlus, FMU), use `--drain`:

```bash
just self-hosted upgrade --to v0.9.0 --drain
```

`--drain` stops accepting new runs, waits for active runs to finish (default 5 min, configurable), then proceeds with the upgrade. Recommended for long-running validators or scheduled upgrade windows.

Release notes flag upgrades that require draining (e.g. destructive migrations, broker protocol changes).

## Rollback policy

Rollback across migrations is **restore-from-backup, not reverse migrations**. Validibot does not promise reversible migrations.

```bash
just self-hosted stop
just self-hosted restore backups/<timestamp-before-upgrade> --components data-only
$EDITOR .envs/.production/.self-hosted/.build  # set VALIDIBOT_IMAGE_TAG back
just self-hosted deploy
```

The upgrade recipe prints the backup path before proceeding so you know which backup to restore from. Release notes mark migrations that are irreversible or long-running.

## Version policy

- **Images are published with immutable version tags and digests.** GHCR is the canonical registry; Docker Hub is a discoverability mirror.
- **Production docs recommend pinning an exact version, not `latest`.** `latest` is fine for evaluation.
- **Validator images are versioned separately** but recorded in doctor output and evidence manifests.
- **Support asks for `<target> doctor --json` and image digests first.**

### Tagging

| Tag | Use | Mutable? |
|---|---|---|
| `0.8.0` | Exact version | Immutable |
| `0.8` | Latest patch in 0.8.x | Mutable (advances on patch releases) |
| `latest` | Latest stable | Mutable |
| `0.8.0@sha256:...` | Digest pin | Immutable |

Production self-hosted docs recommend pinning to an exact `0.8.0` tag or a digest. `latest` is documented for evaluation only.

## Release notes contract

Every release must satisfy the [release-notes policy](release-notes-policy.md):

- **⚠ Required operator action** — checkable list (backup taken, migrations reviewed, downtime window planned).
- **Breaking changes** — explicit, even if "none."
- **Database migrations** — listed with estimated runtime and reversibility note.
- **Validator image changes** — version bumps with breaking-change notes from upstream.
- **Manual operator action** — anything that doesn't auto-apply.

Operators rely on this content **before** upgrading; if breaking changes are buried, they become production incidents.

## Same lifecycle on GCP

```bash
just gcp backup prod
just gcp deploy prod --to v0.9.0
just gcp doctor prod --post-upgrade
just gcp smoke-test prod
```

Same nine-step lifecycle. Cloud Run shifts traffic between revisions, so HTTP stays up except during the migration job. Cross-target parity means we exercise the same flow on our own infrastructure before customers hit edge cases.

## See also

- [Backups](backups.md) — automatic before upgrade
- [Restore](restore.md) — rollback path
- [Release Notes Policy](release-notes-policy.md) — what every release announces
- [Doctor Check IDs](doctor-check-ids.md) — what pre-flight checks
- [Operator Recipes](operator-recipes.md) — full recipe reference

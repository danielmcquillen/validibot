# Resetting an Environment

Sometimes you need to wipe a deployment back to a clean slate — clearing out
demo data before a launch, resetting a staging box that has drifted, or
starting a self-hosted instance over. The
[`reset_system`](../../../validibot/core/management/commands/reset_system.py)
management command does exactly that, and the
`just gcp reset-system` recipe wraps it for deployed environments.

This is a destructive, irreversible operation. Read this whole page before you
run it against anything you care about.

## What it does

`reset_system` deletes all of the operational data an instance accumulates and
then rebuilds the validator catalogue from the current code:

- **Deletes** every validation run, submission (including the uploaded files in
  storage), workflow, project, and validator.
- **Rebuilds** the system validators from their config declarations, at
  version 1, and re-seeds their resource files (weather data and the like).
- **Preserves** your users and organizations. Nothing about identity or
  membership is touched.

So after a reset you still log in as the same people in the same orgs, but
every workflow, project, run, and uploaded file is gone, and the validator
catalogue looks like a fresh install.

## How it keeps you safe

Three guards stand between a stray command and an empty database.

**Dry-run is the default.** Running the command with no confirmation phrase
prints a plan — the target settings module, the database it is pointed at, and
a count of every entity it would delete — and then stops without changing
anything. A bare `just gcp reset-system prod` is always safe to run just to
look.

**A typed phrase is required to delete.** The command only proceeds when you
pass `--confirm RESET-EVERYTHING` exactly. A wrong phrase is treated as an
error, never as a quiet "did nothing" — a typo during an incident should be
loud, not silently mistaken for success.

**It is atomic on the database.** The deletes and the validator rebuild run
inside a single transaction. If the rebuild fails for any reason, the deletes
roll back and the instance is left exactly as it was. Storage (GCS) file
deletions are irreversible, so they happen only *after* that transaction has
safely committed.

### Why a phrase and not a live prompt on GCP

On GCP the command runs as a Cloud Run Job, which has no interactive terminal.
A live "type yes to continue" prompt would have nothing to read from and would
crash. That is why the real gate is the `--confirm` argument rather than a
prompt. When you *do* run the command from a normal terminal — a local shell or
an attached container — it adds a second live prompt as a courtesy, asking you
to retype the phrase. The `just` recipe passes `--noinput` for the GCP path so
that extra prompt is skipped there.

## Running it on GCP production

The recipe runs against whichever image is **currently deployed**. The
`reset_system` command only exists in an image once you have deployed the code
that contains it, so the first step is always to deploy.

```bash
cd ~/projects/validibot/validibot
source .envs/.production/.google-cloud/.just     # loads the prod gcp config
```

**1. Deploy the code that contains the command.** No secret changes are needed
unless you changed environment variables.

```bash
just gcp deploy-all prod
```

**2. Take an on-demand database backup.** There is no undo, and the command also
purges files from storage, so capture a restore point first.

```bash
gcloud sql backups create \
  --instance=validibot-db \
  --project=<your-gcp-project> \
  --description="pre-reset $(date +%F)"
```

If you would rather click than type, the Cloud SQL console does the same thing
under your production instance: **Backups → Create backup**.

**3. Dry-run, and read the plan.** This is your last checkpoint before anything
is deleted. Confirm the `Database:` line really is production and the counts
look like production.

```bash
just gcp reset-system prod
```

```text
Validibot system reset
  Settings module: config.settings.cloud
  Database:        validibot @ /cloudsql/...
  Will DELETE:
    - 412 validation runs
    - 388 submissions
    - 57 workflows
    - 120 projects
    - 34 validators
  Will REBUILD: system validators (from current configs) + resource files
  Will PRESERVE: users, organizations
DRY RUN — nothing was deleted. Re-run with --confirm "RESET-EVERYTHING" to perform the reset.
```

**4. Run it for real.**

```bash
just gcp reset-system prod confirm
```

This expands to `reset_system --confirm RESET-EVERYTHING --noinput`, runs as a
one-shot Cloud Run Job, streams the logs back, and cleans the job up afterwards.

**5. Confirm it finished.** The streamed logs should end with a per-entity
breakdown and a success line:

```text
  Deleted 388 submission row(s).
  Deleted 57 workflow row(s).
  Deleted 120 project row(s).
  Deleted 34 validator row(s).
  Recreated 7 baseline validator(s).
  Purged 800 storage object(s).
System reset complete.
```

The same recipe works for the other environments — just swap `prod` for `dev`
or `staging`.

## Running it locally or in an attached shell

For local development, or inside a container where you have a shell, call the
command directly:

```bash
python manage.py reset_system                            # dry-run preview (default)
python manage.py reset_system --confirm RESET-EVERYTHING # actually run
```

From a real terminal the command will prompt you a second time to retype the
phrase before it proceeds. Pass `--noinput` to skip that, or `--dry-run` to
force a preview even when the phrase is present.

## A note on validator versions

After a reset, every system validator is rebuilt at **version 1**. The
catalogue's config declarations are the source of truth for that, so the reset,
`setup_validibot`, and `sync_validators` all agree on version 1 — there is no
risk of a later sync re-introducing higher-numbered rows. If you are bringing an
older database in line, run `reset_system` rather than a bare `sync_validators`:
the reset wipes the validators first, whereas syncing without wiping would add
version 1 rows alongside any older ones still in the table.

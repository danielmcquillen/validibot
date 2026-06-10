# Doctor check IDs

The `validibot doctor` command (`./manage.py check_validibot`) emits structured findings, each tagged with a short stable code so operators can search documentation, support transcripts, and changelogs for them. This page describes the numbering scheme and how to look up a specific ID.

## How to read a check ID

Every ID has the shape `VBnnn`:

- `VB` — the project prefix (Validibot)
- `nnn` — three-digit code, where the leading digit identifies the category

The category prefix never changes — once a check ID is published, it is a stable contract. New checks within a category get the next free number; obsolete checks remain reserved (do not reuse retired IDs).

## Categories

| Range  | Category                      | Examples of what's checked                                |
| ------ | ----------------------------- | --------------------------------------------------------- |
| `VB0xx` | Settings / configuration      | `DEBUG`, `SECRET_KEY`, `ALLOWED_HOSTS`, CSRF origins, admin URL, secure cookies, OS version |
| `VB1xx` | Database                      | Connection, applied migrations, Postgres version          |
| `VB2xx` | Storage                       | Media/data storage configuration and reachability         |
| `VB3xx` | Docker / containers           | Docker reachable, version, installation source            |
| `VB4xx` | Background tasks / backups    | Celery broker, Beat schedules, restore-test recency       |
| `VB5xx` | Cache                         | Cache backend configured and reachable                    |
| `VB6xx` | Email                         | Email backend configuration                               |
| `VB7xx` | Validators                    | System validators present/enabled, backend image policy   |
| `VB8xx` | Site / roles / initial data   | Site object + domain, role seeding                        |
| `VB9xx` | Network / provider            | DNS, volume mounts, monitoring agent (e.g. DigitalOcean)  |

Run `validibot doctor --json | jq '.checks[].id'` against a working install to enumerate the live IDs in your release.

## Lookup procedure

When an operator reports a `VBnnn` finding:

1. Search the codebase: `grep -rn '"VBnnn"' validibot/core/management/commands/check_validibot.py`. The hit shows the check's category, message, and fix-hint.
2. Search the changelog: each release that introduces a new ID mentions it in `CHANGELOG.md`.
3. Search this dev-docs site for the ID; deeper guidance for trust-critical checks lives alongside the relevant feature doc (e.g. `VB711` lives in [Validator Containers](../validator_jobs_cloud_run.md)).

## Severity levels

Every check returns one of five severities (plus `skipped`):

- `ok` — the deployment satisfies this check.
- `info` — informational, no action recommended.
- `warn` — a soft issue; passes by default but `--strict` promotes it to a failure.
- `error` — a real problem; doctor exits non-zero.
- `fatal` — reserved for problems that invalidate the whole run (e.g. a commercial app named in `INSTALLED_APPS` whose package isn't installed); exits non-zero.
- `skipped` — the check could not run (missing prerequisite, environment-gated, etc.).

The doctor's exit code is:

- `0` — every check is `ok`, `info`, or `skipped` (or `warn` without `--strict`)
- non-zero — at least one `error`/`fatal` (or `warn` with `--strict`)

## Adding a new check ID

1. Pick the next free number in the relevant category.
2. Add a `self._add_result("VBnnn", ...)` call in the appropriate `_check_*` method in `validibot/core/management/commands/check_validibot.py`.
3. Add a test in `validibot/core/tests/test_check_validibot.py` that exercises the failure path so the structured output is pinned.
4. Mention the new ID in the release notes / `CHANGELOG.md` under "Added".

Do not reuse retired IDs — operators may search for them in old support transcripts. Reserved-but-retired IDs should be left as comments in the source file noting the retirement reason.

## See also

- [Validator Containers](../validator_jobs_cloud_run.md) — the `VB7xx` series with the image-pinning policy in detail.
- [Trust Architecture](../overview/trust-architecture.md) — the invariants the doctor verifies.
- [Self-hosting Overview](https://github.com/danielmcquillen/validibot/blob/main/docs/operations/self-hosting/overview.md) — operator-facing guide that points at doctor checks for go-live.

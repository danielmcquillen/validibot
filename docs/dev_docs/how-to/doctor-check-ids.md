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
| `VB0xx` | Doctor framework / version    | Schema version, environment metadata, target / stage      |
| `VB1xx` | Core Django configuration     | `DEBUG`, `ALLOWED_HOSTS`, `SECRET_KEY`, time zone         |
| `VB2xx` | Security headers / transport  | HSTS, CSP, CSRF trusted origins, SSL redirect             |
| `VB3xx` | Database / migrations         | Connection, applied migrations, postgres version          |
| `VB4xx` | Backups / restore drills      | Backup rotation, restore-drill recency, archive integrity |
| `VB5xx` | Auth / OIDC / sessions        | OIDC keys, signing key, session backend                   |
| `VB6xx` | Storage / media               | `DATA_STORAGE_ROOT`, GCS bucket reachability              |
| `VB7xx` | Validators                    | Validator-backend image policy, system validators present |
| `VB8xx` | Provider-specific (e.g. GCP)  | DigitalOcean droplet metadata, GCP project + region       |

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
- `error` — a real problem; doctor exits with code 1.
- `critical` — security-relevant problem; doctor exits with code 1 and the message is shown more prominently.
- `skipped` — the check could not run (missing prerequisite, environment-gated, etc.).

The doctor's exit code is:

- `0` — every check is `ok`, `info`, or `skipped` (or `warn` without `--strict`)
- `1` — at least one `error`/`critical` (or `warn` with `--strict`)

## Adding a new check ID

1. Pick the next free number in the relevant category.
2. Add a `self._add_result("VBnnn", ...)` call in the appropriate `_check_*` method in `validibot/core/management/commands/check_validibot.py`.
3. Add a test in `validibot/core/tests/test_check_validibot.py` that exercises the failure path so the structured output is pinned.
4. Mention the new ID in the release notes / `CHANGELOG.md` under "Added".

Do not reuse retired IDs — operators may search for them in old support transcripts. Reserved-but-retired IDs should be left as comments in the source file noting the retirement reason.

## See also

- [Validator Containers](../validator_jobs_cloud_run.md) — the `VB7xx` series with the image-pinning policy in detail.
- [Trust Architecture](../overview/trust-architecture.md) — the invariants the doctor verifies.
- [Self-hosting Overview](../../operations/self-hosting/overview.md) — operator-facing guide that points at doctor checks for go-live.

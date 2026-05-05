# Smoke test

A smoke test is the cheapest, fastest answer to "is this deployment fundamentally functional?" Doctor catches *configuration* problems (settings missing, DB unreachable, storage not writable). Smoke test catches *runtime* problems (queue broken, worker not picking up jobs, validator can't actually process a payload). Together they form the operator's confidence loop after install, upgrade, or restore.

This work landed in Phase 2 of the [boring self-hosting ADR](https://github.com/validibot/validibot-project/blob/main/docs/adr/2026-04-27-boring-self-hosting-and-operator-experience.md).

## Commands

```bash
just self-hosted smoke-test                    # human-readable output
just self-hosted smoke-test --json             # validibot.smoke-test.v1 schema
just self-hosted smoke-test --timeout-seconds 30

just gcp smoke-test prod                       # runs as a Cloud Run Job
just gcp smoke-test prod --json
```

Self-hosted is single-stage per VM, so it takes no stage argument. The GCP recipe takes a stage and runs the smoke test as a Cloud Run Job against the deployed environment — same Cloud SQL connection, same Secret Manager wiring as the live web service, so what passes here is genuinely representative of production.

## What the smoke test does

The recipe shells into `python manage.py smoke_test` inside the running deployment and walks through six checks (each with a stable check ID for documentation linkage):

| ID | Check | What it verifies |
|---|---|---|
| **ST001** | Demo fixtures | Demo org / user / workflow / step / ruleset exist (idempotent — `get_or_create`) |
| **ST002** | Submission | A new submission with the demo payload is creatable in the deployed database |
| **ST003** | Launch | `ValidationRunService.launch()` accepts the run and dispatches it to the worker |
| **ST004** | Run execution | The run reaches a terminal status within `--timeout-seconds` (default 120s) |
| **ST005** | Run outcome | Terminal status is `SUCCEEDED` with zero findings |
| **ST006** | Signed credential | (Pro only) signed credential round-trips against the local JWKS endpoint |

A failing check stops the cascade only if its result is `FATAL` (e.g. demo fixtures couldn't be created — nothing later can run). Otherwise the smoke test runs every step it can and reports each one's outcome distinctly.

## What "demo data" looks like

The smoke test creates a small set of fixtures with deterministic identifiers, prefixed `smoke-test-` so they're unambiguous in the UI:

| Object | Slug / username | Human name |
|---|---|---|
| Organization | `smoke-test-org` | `Smoke Test [Demo]` |
| User | `smoke-test-user` | `Smoke Test [Demo]` |
| Workflow | `smoke-test-json-schema` | `Smoke Test JSON Schema [Demo]` |
| Ruleset | (auto) | `Smoke Test JSON Schema [Demo]` |

The user has an unusable password — it's never supposed to log in interactively. The "[Demo]" suffix makes demo data easy to filter out in reporting.

**Re-running the smoke test does not duplicate this data.** Each invocation reuses the existing fixtures via `get_or_create`. A fresh `ValidationRun` (and `Submission`) is created on each run so the ValidationRun history shows real timing data, but the surrounding fixtures persist.

## The demo validation

The smoke test runs the simplest possible JSON-Schema validation: one required string field with a constant value.

**Schema:**
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "smoke_test": {"type": "string", "const": "ok"}
  },
  "required": ["smoke_test"],
  "additionalProperties": false
}
```

**Payload:**
```json
{"smoke_test": "ok"}
```

The payload satisfies the schema by construction, so a healthy validator produces zero findings. Any deviation (validator code regression, fixture drift, finding-count miscount) shows up as a non-zero findings count and trips ST005's WARN/ERROR path.

## Distinguishing failure modes

Three different terminal outcomes mean three different fix paths. The check messages call them out explicitly:

| ST005 message | Meaning | Where to look |
|---|---|---|
| "Run succeeded with zero findings" | Healthy. Pipeline works end-to-end. | Nothing to do. |
| "Run succeeded but reported N finding(s)" | Validator returned a different verdict than the schema/payload should produce. Fixture drift or validator regression. | Inspect the run by ID; the demo schema and payload are constants in `smoke_test.py`. |
| "Run failed with N finding(s)" | Validator ran end-to-end but reported issues. Same investigation path as above. | Same as above. |
| "Run failed with a system error" | Pipeline broke before findings could be reported. Worker / dispatcher / config issue. | `just self-hosted doctor` first; then `just self-hosted logs-service worker`. |
| "Run did not reach terminal status within Ns" (ST004) | Worker isn't picking up jobs. | `just self-hosted logs-service worker`; `just self-hosted doctor` for VB401-VB499 task-queue checks. |

The distinction between "validator-level failure" (findings present) and "system failure" (no findings, status FAILED) is what makes the smoke test more specific than the doctor — the doctor catches *configuration* errors, the smoke test catches *runtime* errors that doctor doesn't see.

## JSON output

`--json` emits a stable schema (`validibot.smoke-test.v1`) for dashboards and CI pipelines:

```json
{
  "schema_version": "validibot.smoke-test.v1",
  "generated_at": "2026-05-05T14:30:22.123456+00:00",
  "target": "self_hosted",
  "stage": null,
  "passed": true,
  "results": [
    {
      "id": "ST001",
      "category": "fixtures",
      "name": "Demo fixtures",
      "status": "ok",
      "message": "Demo org, user, workflow ready.",
      "details": {"org_slug": "smoke-test-org", "workflow_id": 42, ...},
      "fix_hint": null
    },
    ...
  ]
}
```

Schema rules:

- **Additive fields stay v1.** New optional keys can be added freely.
- **Removing or renaming any key requires a v2 bump.** Dashboards consume the v1 schema; renaming silently breaks them.
- **`status` values come from a closed set:** `ok`, `info`, `warn`, `error`, `fatal`, `skipped`.

## Exit code

The command exits 0 if every result is non-blocking (`ok`, `info`, `warn`, `skipped`), 1 if any result is `error` or `fatal`. CI pipelines and the upgrade recipe rely on this — a non-zero exit means the smoke test did not pass and the deployment shouldn't be considered healthy.

## When to run it

The smoke test is fast (a few seconds in test mode, under a minute on Compose / Cloud Run with cold-start). Run it:

- **After installing for the first time.** `just self-hosted bootstrap` runs `doctor` automatically; follow up with smoke-test before pointing real workflows at the install.
- **After an upgrade.** Phase 4's upgrade recipe will call smoke-test as the final verification step. Until that lands, run it manually after every `just self-hosted update`.
- **After a restore drill.** Restore writes the `.last-restore-test` marker (silencing doctor's VB411), but the marker doesn't actually verify the restored deployment can validate things. Smoke test does.
- **On a schedule.** A daily cron entry catches problems that slip past doctor — e.g. a worker that's running but not picking up jobs.

```cron
0 */6 * * * cd /srv/validibot/repo && just self-hosted smoke-test --json >> /var/log/validibot-smoke-test.log 2>&1
```

## What the smoke test does NOT verify

The smoke test exercises one validator (JSON Schema) on one tiny payload. It does NOT verify:

- **Advanced validators (EnergyPlus, FMU).** Those require their backend container images, which aren't part of the JSON-Schema smoke test path. A future Pro-edition smoke test may add an advanced-validator step. Until then, run a real workflow against an advanced validator to verify those are healthy.
- **High-load behavior.** The smoke test runs one validation. Capacity tests are a separate concern.
- **Authentication / authorization end-to-end.** The smoke test creates its demo user with a deterministic slug and bypasses login. The doctor catches auth-config problems (CSRF, allowed hosts, MFA settings).
- **External integrations.** Webhooks, Slack notifications, signed-credential issuance — those have their own per-feature tests.

A passing smoke test means "the basic validation pipeline works end-to-end." It's a strong necessary condition for a healthy deployment, not a sufficient one.

## See also

- [Doctor Check IDs](doctor-check-ids.md) — the configuration-side counterpart; doctor's VB411 catches "no restore drill recorded" which complements the smoke test's runtime check
- [Backups](backups.md) — quarterly drill explicitly includes running smoke test on the restore environment
- [Upgrades](upgrades.md) — Phase 4 will gate the upgrade recipe on smoke test passing
- [Operator recipes](operator-recipes.md) — the full just recipe surface

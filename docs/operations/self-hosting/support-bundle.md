# Support Bundle

The support bundle is a redacted archive that lets Validibot support help with an issue without you sending raw model files, secrets, or signing keys. This page covers what's in the bundle, what's redacted, and how the support workflow uses it.

This work lands in Phase 6 of the boring-self-hosting ADR.

## Why this exists

For a 6k–24k/year self-hosted product, the buyer is risk averse. They cannot send raw project data to a vendor as a debugging step. Without a redacted, structured way to share state, support becomes guesswork: "What does your config look like? Can you paste a log?"

The support bundle solves that. One command produces an archive that contains everything Validibot support actually needs — and excludes everything Validibot support should not see.

## Generate a bundle

```bash
just self-hosted collect-support-bundle
```

Output: `support-bundle-<timestamp>.zip` in the current directory.

## What's in the bundle (self-hosted)

```text
support-bundle-<timestamp>.zip
  doctor.json                 # full doctor output, machine-readable
  versions.txt                # Validibot version, validator image versions, Docker version, OS
  docker-compose.resolved.yml # the resolved Compose configuration (with secrets redacted)
  service-status.txt          # `docker compose ps` output
  recent-web.log              # last 1000 lines of web container logs
  recent-worker.log           # last 1000 lines of worker container logs
  recent-validator.log        # last 1000 lines of validator runner logs
  disk-usage.txt              # `df -h` and per-data-dir usage
  migration-state.txt         # `python manage.py showmigrations` output
  validator-manifests.json    # validator backend manifests
```

GCP variant substitutes `cloud-run-revision.txt`, `cloud-sql-state.txt`, `recent-cloud-logs.txt`, etc., but keeps the same schema and redaction rules.

## What's redacted

The bundle's redaction rules apply to **every** included file:

- **secrets and passwords** — `DJANGO_SECRET_KEY`, database passwords, OIDC client secrets, etc. → `[REDACTED]`
- **API tokens** — bearer tokens, CLI tokens → `[REDACTED]`
- **signing keys** — Pro signing keys, JWKS private material → `[REDACTED]`
- **raw submission contents** — uploaded model files, validation inputs → replaced with hashes and sizes
- **environment variable values** — sensitive keys keep their names, values become `[REDACTED]`

What you'll actually see in a redacted file:

```yaml
# docker-compose.resolved.yml (excerpt)
services:
  web:
    environment:
      DJANGO_SECRET_KEY: "[REDACTED]"
      DATABASE_URL: "[REDACTED]"
      SITE_URL: "https://validibot.example.org"  # not sensitive, kept
```

```text
# recent-worker.log (excerpt)
2026-04-27T12:00:01Z INFO Validation run started
  run_id=abc123
  workflow_slug=energyplus-preflight
  workflow_version=3
  submission_size=125342    # size kept
  submission_sha256=...     # hash kept
  # raw bytes never appear in logs by default
```

The redaction list is the same on both self-hosted and GCP — same rules, same library, just target-specific log sources.

## What's NOT in the bundle

Just to be explicit:

- **no signing private keys** (you can verify by inspecting `docker-compose.resolved.yml`);
- **no API tokens** (yours or any user's);
- **no submission file bytes** (size + hash only);
- **no validation finding bodies** (counts and stable codes only — the bundle is for platform-level support, not workflow-level debugging);
- **no Pro license credentials** (the package-index URL is in `.envs/.production/.self-hosted/.build` but the credential portion is redacted).

If your support issue **needs** any of the above to debug, the support email exchange will explicitly request it (e.g. "to debug this validator output, can you confirm the run ID and whether the failing finding has retention=DO_NOT_STORE?"). It is never part of the default bundle.

## The support workflow as a contract

The customer-facing self-hosting docs include a single page that says:

> To open a support ticket, email **support@validibot.com** with the attached output of `just self-hosted collect-support-bundle`. Without the bundle, response time is 1 week. With the bundle, response time is 24 hours (Pro Team), 4 hours (Research/Studio or Organization).

Pattern adopted from Sentry and GitLab — specific data ask gates support response time. Trains operators to send useful information first try, which compounds into faster resolutions and fewer round trips.

### Tier breakdown

| Tier | With bundle | Without bundle |
|---|---|---|
| Community | community channels (issue tracker) | n/a |
| Pro Team | 24 hours | 1 week |
| Research/Studio | 4 hours | 1 week |
| Organization | 4 hours, plus an install-review session per quarter | 1 week |

## Inspect before sending

You can (and should) inspect the bundle before sending it. The redaction is automated; we want you to be able to confirm.

```bash
unzip -l support-bundle-<timestamp>.zip
unzip support-bundle-<timestamp>.zip -d /tmp/support-bundle-inspect
$EDITOR /tmp/support-bundle-inspect/docker-compose.resolved.yml
grep -i -E "(secret|password|key|token)" /tmp/support-bundle-inspect/*.yml /tmp/support-bundle-inspect/*.txt
```

If you find anything sensitive that wasn't redacted, **don't send the bundle** and email support@validibot.com with a description of what you found — that's a redaction bug we'll fix immediately.

## On GCP (our cloud)

```bash
just gcp collect-support-bundle prod
```

We use the same recipe and the same redaction rules for our own incident response. Cross-target parity means we exercise the bundle code path on our own infrastructure before customers hit edge cases.

## On a schedule

Don't generate support bundles on a cron — they include log windows that mean nothing without a specific incident time. Generate one when:

- you're opening a support ticket;
- you're investigating an issue;
- you're documenting a state for a post-incident review;
- you're handing off operations to another team member.

## See also

- [Doctor Check IDs](doctor-check-ids.md) — `doctor --json` output is in the bundle
- [Operator Recipes](operator-recipes.md) — full recipe reference
- [Security Hardening](security-hardening.md) — what data the bundle is designed to protect

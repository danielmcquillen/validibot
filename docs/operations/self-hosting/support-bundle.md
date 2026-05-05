# Support bundle

The support bundle is a redacted archive that lets Validibot support help with an issue without you sending raw model files, secrets, or signing keys. This page covers what's in the bundle, what's redacted (and how), and how the support workflow uses it.

This work landed in Phase 6 of the [boring self-hosting ADR](https://github.com/validibot/validibot-project/blob/main/docs/adr/2026-04-27-boring-self-hosting-and-operator-experience.md).

## Why this exists

For a 6k–24k/year self-hosted product, the buyer is risk-averse. They cannot send raw project data to a vendor as a debugging step. Without a redacted, structured way to share state, support becomes guesswork: "What does your config look like? Can you paste a log?" — followed by three rounds of follow-up.

The support bundle solves that. One command produces an archive that contains everything Validibot support actually needs — and excludes everything Validibot support should not see.

## Generate a bundle

```bash
just self-hosted collect-support-bundle
just self-hosted collect-support-bundle --output /tmp/my-bundle.zip   # custom path

just gcp collect-support-bundle prod
just gcp collect-support-bundle prod --output /tmp/gcp-bundle.zip
```

Default output: `support-bundles/support-bundle-<host>-<timestamp>.zip`. The directory is created if missing; nothing else is written to disk besides the final zip.

## What's in the bundle

Both substrates produce the same conceptual shape. File names differ slightly because the substrates produce different host-side artefacts.

### Self-hosted

```text
support-bundle-<host>-<timestamp>.zip
  README.txt              # what's in here, what's not, how to inspect
  app-snapshot.json       # validibot.support-bundle.v1 schema (Django side)
  service-status.txt      # `docker compose ps` output
  recent-web.log          # last 200 lines of web container logs
  recent-worker.log       # last 200 lines of worker container logs
  recent-postgres.log     # last 200 lines of postgres container logs
  disk-usage.txt          # `df -h` output
  validators.txt          # validator backend inventory (Phase 5 recipe output)
  versions.txt            # Docker / OS / just versions on the host
```

### GCP

```text
support-bundle-<stage>-<timestamp>.zip
  README.txt              # same kit-shipped README
  app-snapshot.json       # validibot.support-bundle.v1 schema (from a Cloud Run Job)
  web-service.txt         # `gcloud run services describe`
  worker-service.txt      # `gcloud run services describe`
  sql-instance.txt        # `gcloud sql instances describe`
  recent-cloud-logs.txt   # `gcloud logging read` (1h, severity≥WARNING)
  versions.txt            # gcloud + deployed image
```

The `app-snapshot.json` is identical in shape across substrates — it's produced by the same `collect_support_bundle` Django management command. Only the host-side capture differs.

## The `app-snapshot.json` schema

The Django-side data is captured into a single JSON document conforming to `validibot.support-bundle.v1`. Top-level shape:

```json
{
  "schema_version": "validibot.support-bundle.v1",
  "captured_at": "2026-05-05T14:30:22.123456+00:00",
  "versions": {
    "validibot_version": "v0.4.0",
    "python_version": "3.13.1",
    "postgres_server_version": "16.0 (Debian ...)",
    "target": "self_hosted",
    "stage": null
  },
  "migrations": {
    "head": {
      "workflows": "0018_add_workflow_publish_invariants",
      "validations": "0047_alter_validationrun_source_choices"
    }
  },
  "settings": [
    {"name": "DEBUG", "value": false, "redacted": false},
    {"name": "ALLOWED_HOSTS", "value": ["validibot.example.com"], "redacted": false},
    {"name": "SECRET_KEY", "value": "[REDACTED]", "redacted": true},
    {"name": "DATABASE_PASSWORD", "value": "[REDACTED]", "redacted": true},
    ...
  ],
  "outbound_calls": {
    "sentry_enabled": false,
    "posthog_enabled": false,
    "email_configured": true,
    "runtime_license_check_enabled": false
  },
  "validators": [
    {"slug": "energyplus", "validation_type": "ENERGYPLUS", "is_system": true, "image": null},
    ...
  ],
  "doctor": { ... }   // embedded validibot.doctor.v1 output
}
```

The schema is **frozen and validated** — Pydantic rejects unknown fields and version mismatches at parse time. Future support tooling can rely on shape stability.

## How redaction works

Two layers, both in `validibot/core/support_bundle.py`:

### Layer 1 — name-based

A setting whose name contains any of these fragments (case-insensitive substring match) gets `[REDACTED]` regardless of value:

```text
SECRET, PASSWORD, PASSWD, TOKEN, KEY, ENCRYPTION, PRIVATE,
CREDENTIAL, AUTH, DSN, WEBHOOK, SIGNING
```

A short allowlist of well-known false-positives passes through (`USE_AUTH`, `AUTH_USER_MODEL`, `AUTHENTICATION_BACKENDS`, `PASSWORD_HASHERS`, `AUTH_PASSWORD_VALIDATORS`).

### Layer 2 — value-shape (defense in depth)

If a setting's name didn't trip the name check but its value *looks* like a credential, it's still redacted. The patterns:

- 32+ character hex (likely a SHA-256ish secret)
- JWT prefix (`eyJ...`)
- PEM private key (`-----BEGIN ... PRIVATE KEY-----`)
- Bearer tokens (`Bearer abc...`)
- URLs with embedded basic auth (`https://user:pass@host/...`)

False positives — a non-secret value that happens to match a pattern — are acceptable. False negatives (a real secret slipping through) are not.

### What's NOT in the bundle (intentionally excluded)

- **Raw submission contents.** Operator-uploaded files never enter the bundle.
- **Validation findings or evidence bundles.** Those are separate artefacts; share via your existing channel only when support explicitly asks.
- **Database contents.** The bundle is metadata-only — no SQL dumps. For a data dump, use `just self-hosted backup` and share that out-of-band.
- **Signing private keys.** Excluded by name (`SIGNING_*`) and by location (`/run/validibot-keys/`).
- **Pro license credentials.** Build-time `.env` values for the private package index are redacted by name.

## Inspecting before you send

You can — and should — inspect the bundle before sending it. The redaction is automated; the inspection step lets you confirm.

```bash
unzip -l support-bundles/support-bundle-*.zip                       # list contents
unzip -p support-bundles/support-bundle-*.zip app-snapshot.json | jq .   # read app-snapshot
unzip support-bundles/support-bundle-*.zip -d /tmp/inspect           # extract for browsing

# Quick sanity check that no obvious secret leaked. Should produce
# only matches that are themselves [REDACTED] markers.
grep -irE "secret|password|token|api_key" /tmp/inspect/ | grep -v "REDACTED"
```

If you find anything sensitive that wasn't redacted, **don't send the bundle** — email support@validibot.com with a description of what you found and the bundle's `bundle_id`. That's a redaction bug we'll fix immediately, before anyone else's bundle is at risk.

## The support workflow as a contract

Sending the bundle is the price of fast support. Without it, support is guesswork — and guesswork takes weeks.

> To open a support ticket, email **support@validibot.com** with the attached output of `just self-hosted collect-support-bundle`. Without the bundle, response time is 1 week. With the bundle, response time is **24 hours (Pro Team)** or **4 hours (Research/Studio or Organization)**.

Pattern adopted from Sentry and GitLab — specific data ask gates support response time. Trains operators to send useful information first try, which compounds into faster resolutions and fewer round-trips.

### Tier breakdown

| Tier | With bundle | Without bundle |
|---|---|---|
| Community | community channels (GitHub issues) | n/a |
| Pro Team | 24 hours | 1 week |
| Research / Studio | 4 hours | 1 week |
| Organization | 4 hours + quarterly install review | 1 week |

The "1 week" without-bundle SLA is the same across paid tiers because without the bundle, support staff start the diagnosis from zero — paid tiers don't accelerate that.

## When to generate one

Don't generate support bundles on a cron — they include log windows that mean nothing without a specific incident time. Generate one when:

- you're opening a support ticket
- you're investigating an issue and want a snapshot to compare against later
- you're documenting state for a post-incident review
- you're handing off operations to another team member

## When the deployment is broken

If the web container is down (`docker compose ps` shows `web` exited), the recipe still produces a bundle — it just skips the app-side snapshot and includes an `app-snapshot-error.log` instead. Host-side artefacts (compose ps, logs, disk usage, validator inventory) are captured regardless.

A "deployment is broken" bundle is exactly the kind support most needs.

## Cross-target parity

The same management command and the same redaction module run on both targets. We use the same support bundle command for our own GCP incident response — so when we change the redaction rules (e.g. adding a new sensitive name fragment), it lands on our cloud first, not on a customer's deployment.

## See also

- [Doctor Check IDs](doctor-check-ids.md) — `doctor --json` output is embedded in `app-snapshot.json`
- [Operator Recipes](operator-recipes.md) — full recipe reference
- [Security Hardening](security-hardening.md) — what data the bundle is designed to protect
- [Backups](backups.md) — for data dumps (which are NOT in the support bundle)

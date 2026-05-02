# Release Notes Policy

Every Validibot release intended for self-hosted pilots ships with release notes that follow this template. Operators rely on this content **before** upgrading; if breaking changes are buried, they become production incidents.

This work lands in Phase 6 of the boring-self-hosting ADR. Pattern adopted from Plausible's CHANGELOG and GitLab's "Manual operator action required" callouts.

## Why this is a contract

Release notes are not marketing copy. They are an operator-facing contract that says:

- here is what's safe to upgrade to;
- here is what requires preparation;
- here is what will break if you skip versions.

A release without proper notes is not pilot-ready. The first published release after Phase 4 of the boring-self-hosting ADR (upgrade workflow) ships uses this template, verified before tagging.

## The required sections

Every release announcement must include these five sections, in this order:

### ⚠ Required operator action

A checkable list of things the operator must do **before, during, or immediately after** the upgrade. Examples:

- [ ] Take a backup before upgrading (`just self-hosted backup`).
- [ ] Review the migrations table below; if any migration is marked irreversible or long-running, plan a downtime window.
- [ ] If you're below v0.7.0, upgrade to v0.7.0 first (see Strict Upgrade Path below).

If there's nothing required, the section says **"None."** explicitly. Silence is not allowed.

### Breaking changes

Every breaking change, named explicitly. Examples:

- `WorkflowStep.config` schema changes — the `assertion_targets` field becomes a list of objects rather than a list of strings. Existing rows are auto-migrated; custom code that builds workflows programmatically must update.
- The MCP `workflow_ref` format adds an optional version segment. Old refs (`org/slug`) still resolve to the latest version; new refs (`org/slug@N`) pin a specific version.

If there are no breaking changes, the section says **"None."** explicitly.

### Database migrations

A list of migrations included in this release, with:

- migration ID and Django app;
- estimated runtime on a baseline deployment;
- reversibility note.

Example:

| Migration | App | Estimated runtime | Reversible? |
|---|---|---:|---|
| `0042_add_validator_semantic_digest` | `validations` | < 1 sec | Yes |
| `0043_add_resource_content_hash` | `validations` | < 1 sec | Yes |
| `0044_add_run_evidence_artifact` | `validations` | < 1 sec | Yes |
| `0045_backfill_workflow_contract_digests` | `workflows` | ~30 sec per 1000 workflows | **No** — drops a temporary table after backfill |

Long-running migrations get a clear estimate. Irreversible migrations get a **bold "No"** on reversibility.

### Validator image changes

Validator images are versioned separately from Validibot itself but bundled with `validibot-pro`. List version bumps with their breaking-change notes from upstream:

| Validator | Old version | New version | Notes |
|---|---|---|---|
| EnergyPlus | 24.1.0 | 24.2.0 | EnergyPlus 24.2 release notes link. No envelope schema changes. |
| FMU | 2.0.4 | 2.0.5 | Patch release; no behavioural changes. |

If validator images are unchanged, the section says **"No validator image changes."**

### Manual operator action

Anything that doesn't auto-apply via `just self-hosted upgrade`. Examples:

- *Add a new env var to `.envs/.production/.self-hosted/.django`:* `EVIDENCE_BUNDLE_RETENTION_DAYS` — defaults to `90`, override if you need shorter or longer evidence retention.
- *Re-run `sync_validators`* once after the upgrade to populate `semantic_digest` for legacy validator rows.
- *Update reverse proxy:* if you're not using bundled Caddy, adjust your nginx/Traefik config for the new `/.well-known/jwks.json` route (Pro only).

Each item has a clear "do this, then do that" structure. If there's nothing manual, the section says **"None."**

## Optional sections

A release may also include:

- **Highlights** — short summary of what's new for end users.
- **New features** — links to docs.
- **Bug fixes** — list of fixed issues with issue/PR links.
- **Performance** — any measurable improvements.
- **Known issues** — anything caught after RC that operators should be aware of.

These are optional. The five required sections above are not.

## Strict upgrade-path enforcement

When a release requires intermediate stops (e.g. v0.10 requires going through v0.9), the release notes include a **Strict Upgrade Path** section:

> **Strict upgrade path:** From v0.8.x, you must upgrade to v0.9.0 first, then to v0.10.0. Direct v0.8.x → v0.10.0 upgrades are blocked by the `upgrade` recipe.

This matches the runtime check in the `upgrade` recipe — it refuses cross-major-version jumps and prints the documented intermediate stops. See [upgrades.md § Strict upgrade-path enforcement](upgrades.md#strict-upgrade-path-enforcement).

## Format

Markdown, published to:

- the GitHub release page;
- the Validibot docs site;
- the prospects-facing self-hosted deployment guide (Phase 5 of productization).

Each release page is a permanent URL — never edited after publish, except for typo corrections with a "Last updated" footer.

## Review checklist

Before tagging a release intended for self-hosted pilots:

- [ ] **⚠ Required operator action** is filled in (or explicitly "None.").
- [ ] **Breaking changes** is filled in (or explicitly "None.").
- [ ] **Database migrations** lists every migration with runtime + reversibility.
- [ ] **Validator image changes** lists every image version bump (or "No validator image changes.").
- [ ] **Manual operator action** is filled in (or "None.").
- [ ] If a strict upgrade path applies, it's clearly stated.
- [ ] The notes have been reviewed by Daniel before tagging.

This is part of the release checklist in the boring-self-hosting ADR § Manual release checklist.

## Example release notes shape

```markdown
# Validibot v0.9.0 — 2026-06-15

## ⚠ Required operator action

- [ ] Take a backup before upgrading (`just self-hosted backup`).
- [ ] Review the migrations table — `0045_backfill_workflow_contract_digests` is irreversible.

## Breaking changes

- `Validator.semantic_digest` is now required for all system validators. After upgrade, run `sync_validators` to populate digests for any custom validators.

## Database migrations

| Migration | App | Estimated runtime | Reversible? |
|---|---|---:|---|
| `0042_add_validator_semantic_digest` | `validations` | < 1 sec | Yes |
| `0043_add_resource_content_hash` | `validations` | < 1 sec | Yes |
| `0044_add_run_evidence_artifact` | `validations` | < 1 sec | Yes |

## Validator image changes

| Validator | Old version | New version | Notes |
|---|---|---|---|
| EnergyPlus | 24.1.0 | 24.2.0 | EnergyPlus 24.2 release notes — no envelope schema changes. |

## Manual operator action

- Run `python manage.py audit_workflow_versions` after upgrading to surface any legacy-versioning gaps in your existing workflows.

## Highlights

- Evidence bundle MVP shipped (Phase 4 of the trust ADR).
- Trust-boundary hardening complete (Phase 3).
- New `audit_workflow_versions` management command.

## Bug fixes

- ...

## Strict upgrade path

From v0.7.x: upgrade to v0.8.0 first, then to v0.9.0.
```

## See also

- [Upgrades](upgrades.md) — the upgrade recipe and pre-flight checks
- [Operator Recipes](operator-recipes.md) — the full recipe reference

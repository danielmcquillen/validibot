# Validator Images

This page covers the validator image story for self-hosted operators: what's pre-installed, how to inventory them, how to pin versions, how to add custom validators, and how the run-scoped isolation guarantees work.

This work lands in Phase 5 of the boring-self-hosting ADR.

## What's an "advanced validator"?

Validibot has two classes of validator:

- **Simple validators** run synchronously inside the Django process. Examples: JSON Schema, XML Schema, Basic (CEL assertions), AI (LLM-backed checks). They never touch a container.
- **Advanced validators** delegate the heavyweight work to an external Docker container — usually a third-party simulation engine like EnergyPlus or FMU. The Django code dispatches an input envelope and reads the output envelope when the container exits.

Advanced validators are a Pro feature. Community deployments only see simple validators.

For the developer-facing reference on how this works, see the dev-docs companion at `docs/dev_docs/overview/validator_architecture.md`.

## What's pre-installed

The current shipped advanced validators:

| Slug | Backend image (built locally) | Purpose |
|---|---|---|
| `energyplus` | `validibot-validator-backend-energyplus:<git_sha>` | Building energy simulation |
| `fmu` | `validibot-validator-backend-fmu:<git_sha>` | Functional Mock-up Unit simulation |

Self-hosted operators **build validator images locally** from a sibling checkout of `validibot-validator-backends`. There's no public image registry pull for validators (the source is open under AGPL but the images aren't currently pushed to GHCR). Build with:

```bash
just self-hosted validator-build energyplus
just self-hosted validator-build fmu
just self-hosted validators-build-all      # builds both
```

The build stamps OCI labels (`org.opencontainers.image.version`, `revision`, `source`, `io.validibot.validator-backend.slug`) onto the image, so a future `docker inspect` can read the human-readable backend version straight from the image metadata.

## Inventory

```bash
just self-hosted validators
```

Lists every `validibot-validator-backend-*` image on the local Docker daemon, with its OCI version label, content digest, size, and age:

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Validator backends — local Docker daemon
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REPOSITORY                                   TAG       BACKEND VERSION  DIGEST                  SIZE       AGE
-------------------------------------------  --------  -----------------  ----------------------  ---------  ---
validibot-validator-backend-energyplus       abc1234   25.2.0           sha256:f7a3c4d8e2b1...  456MB      2 days ago
validibot-validator-backend-energyplus       latest    25.2.0           sha256:f7a3c4d8e2b1...  456MB      2 days ago
validibot-validator-backend-fmu              abc1234   0.3.29           sha256:9b1c2d3e4f5a...  234MB      2 days ago
validibot-validator-backend-fmu              latest    0.3.29           sha256:9b1c2d3e4f5a...  234MB      2 days ago

Tip: backend version comes from org.opencontainers.image.version
     (set when the image was built from validibot-validator-backends).
     Use the digest for trust-critical verification, not the tag.
```

The "BACKEND VERSION" column is the **human-readable identity** (e.g. EnergyPlus 25.2.0). The "DIGEST" column is the **cryptographic identity** — that's what cosign signs and what trust verification commits to. They serve different audiences:

- **Operator browsing inventory** → reads BACKEND VERSION
- **Audit / cryptographic verifier** → reads DIGEST

## Smoke testing the validation pipeline

There's no separate `validators smoke-test` subcommand — the main `just self-hosted smoke-test` (Phase 2) exercises the JSON Schema validator end-to-end through the same code path real validations use. If that passes, the pipeline (queue, worker, dispatcher) is healthy.

For verifying the *advanced* validators specifically (EnergyPlus, FMU), the operational path is:

1. Run a real workflow against the validator with a known-good input.
2. Inspect the `ValidationRun` outcome.

A failed advanced-validator run points at:

- Docker socket unreachable (self-hosted) or Cloud Run Job invoker permissions (GCP);
- validator image not built locally (see Inventory above) or pull credentials wrong (cloud);
- storage misconfiguration (data root not writable, run workspace can't be created);
- network policy blocking required outbound calls (most validators run with `network=none` by default).

A future `validators smoke-test` recipe could automate this; for the MVP, the operator-driven path is sufficient because most operators have at least one workflow they care about and can run manually.

## Image pinning

Production self-hosted docs recommend pinning validator images by exact version, not `latest`. The Compose stack reads validator images from the `validibot-pro` package metadata — when you upgrade Pro, you get the validator image versions Pro was tested against.

For risk-averse customers who want stricter pinning:

```bash
VALIDATOR_IMAGE_POLICY=digest just self-hosted deploy
```

Policy values:

| Policy | What it does |
|---|---|
| `tag` | Default for community quick-start. Image references like `ghcr.io/validibot/energyplus:24.2.0`. |
| `digest` | Production-recommended. Image references include `@sha256:...` digests pinned at deploy time. |
| `signed-digest` | Future enterprise/high-trust. Requires cosign-verified digests. |

Phase 5 of the trust ADR adds `signed-digest` support and optional cosign verification.

## Run-scoped isolation

Every validator backend runtime gets:

- a per-run input directory mounted **read-only** at `/validibot/input`;
- a per-run output directory mounted **read-write** at `/validibot/output`;
- a tmpfs at `/tmp` for scratch work;
- nothing else from the host.

Default container policy:

- `network_disabled=True` unless the validator manifest explicitly requires network;
- `cap_drop=["ALL"]`;
- `security_opt=["no-new-privileges:true"]`;
- non-root user (UID 1000);
- read-only root filesystem;
- pids, memory, CPU, and timeout limits;
- container labels for cleanup;
- image pinned by digest when policy is `digest` or `signed-digest`.

A buggy or compromised validator backend cannot read other runs' inputs, mutate other runs' outputs, exhaust shared disk, or leak data between runs. See [Security Hardening](security-hardening.md) for the architectural rationale.

## Optional hardening

Documented but not required for MVP:

- **rootless Docker** — run the Docker daemon as a non-root user. Significant security improvement; requires Docker 20.10+ and some kernel tweaks.
- **rootless Podman** — Podman with the Docker-compatible API. Drop-in replacement for Docker socket on systemd hosts.
- **Docker socket proxy** — restrict the worker container's access to Docker to a narrow API subset.
- **gVisor runtime** — sandbox containers with a user-space kernel.
- **Kubernetes Job runner** — alternative to Docker Compose; future hardening track.
- **per-validator seccomp profiles** — fine-grained syscall filtering.
- **egress deny-by-default network policies** — at the network layer rather than the container layer.

The next major hardening track is **two-tier validator trust** (Phase 5):

- **Tier 1 — first-party** (current EnergyPlus, FMU): current Phase 1 hardening.
- **Tier 2 — user-added** (future self-service registration): tier 1 + explicit egress allowlist, tighter resource caps, gVisor or Kata runtime, cosign-signed image required, pre-flight scan.

## Adding custom validators

User-supplied validator backends are not yet supported in the self-hosted MVP. The infrastructure exists (`AdvancedValidator` base class, `ExecutionBackend` abstraction, envelope schema in `validibot-shared`), but the self-service registration flow + tier-2 hardening profile + image scan + cosign verification will land in Phase 5.

If you have a custom validator backend you want to run today, the path is a paid professional services engagement: we build it as a first-party container in `validibot-validator-backends`, ship it as part of `validibot-pro`, and you pull it via the standard upgrade flow. Talk to support.

## Cleanup

Validator containers are short-lived but accumulate as exit artifacts. So do manifested backups past their retention window and old upgrade reports. The `cleanup` recipe walks all of these in one pass:

```bash
just self-hosted cleanup --dry-run    # list candidates without deleting
just self-hosted cleanup              # interactive: list, prompt, delete
just self-hosted cleanup --yes        # cron-friendly: list, delete (no prompt)
```

Three retention scopes, each configurable via env var:

| Scope | Default | Env var override |
|---|---|---|
| Stopped validator containers (filtered by `io.validibot.validator-backend.slug` label) | 24h | `VALIDATOR_RETAIN_HOURS` |
| Manifested backups (read `manifest.json::created_at`) | 30d | `BACKUP_RETAIN_DAYS` |
| Upgrade reports (`backups/upgrades/*/report.json` mtime) | 90d | `UPGRADE_REPORT_RETAIN_DAYS` |

Plus a "bonus pass" that prunes Docker dangling images (always safe — nothing references them).

The recipe **always lists candidates before any deletion**, even without `--dry-run`. The operator sees what will be removed, then confirms (or re-runs with `--yes` for cron). Pattern adopted from Discourse's `./launcher cleanup`.

What `cleanup` does NOT touch:

- Validator backend **images** themselves — re-pulling/re-building is expensive. Use `docker image prune` directly when you genuinely want to reclaim image storage.
- Working-set volumes (Postgres, Redis, `validibot_storage`). Those are part of the live deployment; `clean-all` is the recipe for that.
- Ad-hoc `backup-db` `.sql.gz` dumps at the top of `backups/`. Those are operator-managed; we don't know your retention policy.

A reasonable cron entry:

```cron
0 3 * * 0  cd /srv/validibot/repo && just self-hosted cleanup --yes >> /var/log/validibot-cleanup.log 2>&1
```

Weekly cleanup at 3am Sunday. The log shows what was removed; if nothing matched, the recipe prints "Nothing to clean up." and exits 0.

## Image registry: GHCR primary, Docker Hub mirror

| Registry | Path | Use |
|---|---|---|
| GHCR | `ghcr.io/validibot/<image>` | Canonical source. No pull rate limits. |
| Docker Hub | `validibot/<image>` | Discoverability mirror. Rate-limited (100 anonymous pulls per 6h per IP). |

Self-hosted defaults to GHCR for production (no rate limits). Docker Hub stays as a mirror because most operators look there first.

## See also

- [Install](install.md) — initial setup
- [Upgrades](upgrades.md) — validator images update with `validibot-pro`
- [Security Hardening](security-hardening.md) — full hardening recommendations
- [Doctor Check IDs](doctor-check-ids.md) — VB320/VB321 Docker checks
- [Operator Recipes](operator-recipes.md)
- The dev-docs companion at `docs/dev_docs/overview/validator_architecture.md`

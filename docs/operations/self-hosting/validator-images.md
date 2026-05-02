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

| Slug | Backend image | Purpose |
|---|---|---|
| `energyplus` | `ghcr.io/validibot/energyplus:<version>` | Building energy simulation |
| `fmu` | `ghcr.io/validibot/fmu:<version>` | Functional Mock-up Unit simulation |

The exact versions installed in your deployment depend on your `validibot-pro` version. Use the inventory recipe to check:

```bash
just self-hosted validators list-images
```

Output looks like:

```text
SLUG       VERSION    IMAGE                                          DIGEST
energyplus 24.2.0     ghcr.io/validibot/energyplus:24.2.0            sha256:...
fmu        2.0.4      ghcr.io/validibot/fmu:2.0.4                    sha256:...
```

## Smoke testing validators

```bash
just self-hosted validators smoke-test
```

Runs each advanced validator with a known-good demo input. Useful after upgrading validator images, after Docker daemon restarts, or when troubleshooting.

A failed validator smoke test points at:

- Docker socket unreachable (self-hosted) or Cloud Run Job invoker permissions (GCP);
- image not pulled or pull credentials wrong;
- storage misconfiguration (data root not writable, run workspace can't be created);
- network policy blocking required outbound calls (most validators run with `network=none` by default, but see the per-validator metadata).

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

Validator containers are short-lived but accumulate as exit artifacts. The `cleanup` recipe handles them:

```bash
just self-hosted cleanup --dry-run    # show what would be deleted
just self-hosted cleanup              # actually delete
```

What it cleans:

- stopped validator containers older than `VALIDATOR_RETAIN_HOURS` (default 24h);
- dangling Docker images from previous Compose builds;
- expired backups past retention (default 30d);
- rotated log files past max age.

Always shows a dry-run summary before destructive action, even without `--dry-run`. Operators confirm before deletion. Safe to run on a cron schedule.

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

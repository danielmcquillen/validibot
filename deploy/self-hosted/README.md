# Validibot Self-Hosted Deployment Kit

This directory contains the operator-facing artifacts for running
Validibot on a single Linux VM (DigitalOcean, AWS EC2, Hetzner, on-prem).

If you're new here, **start with**:

- `docs/operations/self-hosting/overview.md` — what self-hosted is and
  how to think about it.
- `docs/operations/self-hosting/providers/digitalocean.md` — the
  ten-step DigitalOcean tutorial (Phase 1 work — outline today, full
  walkthrough lands later).

For day-to-day operations, use the `just self-hosted` recipes from the
repo root (see `just self-hosted --list`).

## Layout

```text
deploy/self-hosted/
  README.md                   ← you are here
  caddy/
    Caddyfile                 ← reverse proxy config (opt-in via Compose profile)
  scripts/
    bootstrap-host            ← prep a fresh VM (Docker, just, user, dirs)
    bootstrap-digitalocean    ← DigitalOcean-tuned bootstrap
```

These two scripts are the **only** scripts in the kit. Everything
else is a `just` recipe — see `just self-hosted --list` from the
repo root.

Why only two scripts? Because they're the only operations that have
to run *before* `just` is installed on the VM. After `bootstrap-host`
finishes, `just` exists, and every subsequent operation
(`check-dns`, `build-pro-image`, `bootstrap`, `deploy`, `doctor`,
`backup`, etc.) is a `just self-hosted <recipe>`. See ADR-2026-04-27
section 4.

## Relationship to the rest of the repo

`deploy/self-hosted/` holds **artifacts** — Caddyfile, helper scripts,
provider tutorials. The actual deploy lifecycle (build, up, doctor,
backup, restore, upgrade) is driven from the repo root via:

```bash
just self-hosted bootstrap   # first-time install
just self-hosted deploy       # upgrades and rebuilds
just self-hosted doctor       # health diagnostic (Phase 1 — stub today)
just self-hosted backup       # application-level backup (Phase 3 — stub today)
just self-hosted --list       # see all recipes
```

The Compose stack itself lives at `docker-compose.production.yml` in
the repo root. The kit doesn't introduce a parallel Compose file; it
adds the operator-facing layer on top.

## Caddy: reverse proxy, off by default

The `caddy/Caddyfile` in this directory is read by an opt-in Compose
service. Most operators bring their own reverse proxy (nginx, Traefik,
Cloudflare Tunnel, hosting-provider load balancer) — for them, the
Caddy profile stays off.

To enable bundled Caddy with auto-TLS:

```bash
COMPOSE_PROFILES=caddy just self-hosted deploy
```

Confirm `SITE_URL` resolves to your VM's public IP first:

```bash
just self-hosted check-dns
```

## ADR reference

See [ADR-2026-04-27: Boring Self-Hosting and Operator
Experience](../../docs/dev_docs/adr/) (also in the
`validibot-project` repo) for the full design rationale.

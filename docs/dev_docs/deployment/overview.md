# Deployment Overview

This section helps you choose the right deployment target for Validibot.

These pages are meant for customer-facing self-host deployments. Hosted-cloud operator workflows belong in the private internal docs, not here.

If you are new to Validibot and just want to get it running, start with [Run Validibot Locally](deploy-local.md).

## The three deployment targets

Validibot has three deployment targets that share the same Django codebase but have different audiences and substrates:

| Target | Substrate | Driver | Audience | Stages |
|---|---|---|---|---|
| **local** | `docker-compose.local.yml` on the developer's laptop | `just local <cmd>` | one Validibot developer testing the app | dev only |
| **self-hosted** | `docker-compose.production.yml` on a single Linux VM | `just self-hosted <cmd>` | a customer running their own copy on DigitalOcean, AWS EC2, on-prem | single env per VM |
| **GCP** | Cloud Run, Cloud SQL, Cloud Tasks, GCS | `just gcp <cmd>` | the Validibot team operating its own cloud | dev / staging / prod |

The same operator capabilities (`bootstrap`, `doctor`, `smoke-test`, `backup`, `restore`, `upgrade`, `collect-support-bundle`) exist for **both self-hosted and GCP**. Cross-target parity is a quality gate: if a recipe exists for self-hosted but not GCP (or vice versa), assume the design is wrong until proven otherwise.

For the architectural rationale, see the boring-self-hosting ADR.

## Choose a target

| Target | Best for | Start here |
| --- | --- | --- |
| Local | first-time evaluation, local sandboxing, development | [Run Validibot Locally](deploy-local.md) |
| Self-hosted | single-host self-hosting on a VPS, VM, or on-prem server | [Self-Hosting Overview](../../operations/self-hosting/overview.md) (operator-facing) or [Deploy with Docker Compose](deploy-docker-compose.md) (developer-facing) |
| GCP | managed cloud deployment on Google Cloud | [Deploy to GCP](deploy-gcp.md) |
| AWS | future target, not yet implemented | [Deploy to AWS](deploy-aws.md) |

## Command style

All deployment targets use the [Just command runner](justfile-guide.md).

Typical commands look like this:

```bash
just local up
just self-hosted bootstrap
just gcp deploy-all dev
```

## Which page should I read first?

Use this shortcut:

- If you want the quickest path to a running app on your machine, read [Run Validibot Locally](deploy-local.md).
- If you want a production deployment on infrastructure you control, read [Deploy with Docker Compose](deploy-docker-compose.md).
- If you want a managed cloud deployment on Google Cloud, read [Deploy to GCP](deploy-gcp.md).
- If you need AWS specifically, read [Deploy to AWS](deploy-aws.md) and plan on using Docker Compose on an AWS host for now.

## Related deployment guides

Once you have chosen a target, these supporting guides become relevant:

- [Environment Configuration](environment-configuration.md)
- [Reverse Proxy Setup](reverse-proxy.md)
- [Docker Compose Deployment Responsibility](docker-compose-responsibility.md)
- [Self-Hosting on DigitalOcean](../../operations/self-hosting/providers/digitalocean.md) — canonical operator-facing tutorial
- [Go-Live Checklist](go-live-checklist.md)

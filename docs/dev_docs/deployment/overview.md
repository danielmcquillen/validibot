# Deployment Overview

This section helps you choose the right deployment target for Validibot.

These pages are meant for customer-facing self-host deployments. Internal `validibot-cloud` and Daniel-specific operator workflows belong in `validibot-project`.

If you are new to Validibot and just want to get it running, start with [Run Validibot Locally](deploy-local.md).

## Choose a target

| Target | Best for | Start here |
| --- | --- | --- |
| Local | first-time evaluation, local sandboxing, development | [Run Validibot Locally](deploy-local.md) |
| Docker Compose | single-host self-hosting on a VPS, VM, or on-prem server | [Deploy with Docker Compose](deploy-docker-compose.md) |
| GCP | managed cloud deployment on Google Cloud | [Deploy to GCP](deploy-gcp.md) |
| AWS | future target, not yet implemented | [Deploy to AWS](deploy-aws.md) |

## Command style

All deployment targets use the [Just command runner](justfile-guide.md).

Typical commands look like this:

```bash
just local up
just docker-compose bootstrap
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
- [Deploying to DigitalOcean](digitalocean.md)
- [Go-Live Checklist](go-live-checklist.md)

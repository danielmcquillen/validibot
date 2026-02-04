# Docker Compose Deployment Responsibility

Validibot's Community edition can be deployed via Docker Compose to any infrastructure you control. That means the operator (you) owns the infrastructure, security posture, and cloud costs. This guide summarizes the practical responsibilities that come with running Validibot in production and follows the same "clear boundaries + safe defaults" approach used by other open-core projects.

## What you’re responsible for

Running Validibot yourself means you control security, data protection, updates, and costs. In practice that usually means TLS at the edge, secure secrets, reliable backups, and resource limits that keep the system stable under load.

## Security checklist (baseline)

Keep the checklist short and repeatable:

- Terminate TLS at the edge and keep internal services private.
- Store secrets outside the repo and rotate them regularly.
- Treat the Docker/Podman socket as root-level access; restrict it to the worker only.
- Use least-privilege credentials for database, storage, and job execution.

## Resource-limit defaults (starting point)

These are safe defaults for single-host deployments. Tune based on your workload.

| Setting | Recommended default | Notes |
| --- | --- | --- |
| Advanced validator CPU | 1 vCPU | Increase for large simulations |
| Advanced validator RAM | 2–4 GB | EnergyPlus/FMU may need more |
| Advanced validator timeout | 30 minutes | Set hard stop for runaway runs |
| Advanced validator concurrency | 2 per host | Cap heavy workloads globally |
| Run workspace | Local SSD | Reduces I/O bottlenecks |

## Cost guardrails

Start with a few simple guardrails: pre-pull validator images, cap heavy-validator concurrency, and use storage lifecycle policies for large artifacts. Monitor CPU/RAM/IO saturation before scaling hosts.

## Operational hygiene

Schedule database backups, rotate secrets on a regular cadence, and apply OS/container security updates quickly. Keep a short incident playbook with the first logs to check and a rollback plan.

## Container cleanup

Validator containers are labeled and automatically cleaned up, but you should understand the cleanup mechanisms:

1. **On-demand cleanup** - Containers are removed immediately after each validation run completes
2. **Periodic sweep** - A background task runs every 10 minutes to remove orphaned containers that exceeded their timeout plus a 5-minute grace period
3. **Startup cleanup** - When the Celery worker starts, it removes any leftover containers from previous runs

If you suspect orphaned containers, run:

```bash
# See what would be cleaned up
python manage.py cleanup_containers --dry-run

# Clean up orphaned containers
python manage.py cleanup_containers

# Force remove ALL managed containers
python manage.py cleanup_containers --all
```

Containers are identified by the `org.validibot.managed=true` label.

## Why this is explicit

Open-core projects that ship Docker Compose deployments set clear expectations about operator responsibilities. We follow the same approach to reduce surprises, encourage safe deployments, and keep support boundaries clear.

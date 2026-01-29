# Self-Hosted Responsibility

Validibot’s Community edition is self-hosted. That means the operator (you) owns the infrastructure, security posture, and cloud costs. This guide summarizes the practical responsibilities that come with running Validibot in production and follows the same “clear boundaries + safe defaults” approach used by other open-core projects.

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

## Why this is explicit

Open-core projects that ship self-hosted deployments set clear expectations about operator responsibilities. We follow the same approach to reduce surprises, encourage safe deployments, and keep support boundaries clear.

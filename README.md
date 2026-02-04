<div align="center">

<img src="https://validibot.com/static/marketing/images/robot_scene_schema_validation.png" alt="Validibot - Data Validation Robot" width="400">

# Validibot

**Open-source data validation engine for building energy models and beyond**

[![Build Status](https://img.shields.io/github/actions/workflow/status/validibot/validibot/ci.yml?branch=main)](https://github.com/validibot/validibot/actions)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
[![Django 5.2](https://img.shields.io/badge/django-5.2-green.svg)](https://djangoproject.com/)

[User Documentation](https://docs.validibot.com/) •
[Developer Documentation](https://dev.validibot.com/) •
[Getting Started](https://docs.validibot.com/getting-started) •
[Community](https://github.com/danielmcquillen/validibot/discussions) •
[Pricing](https://validibot.com/pricing)

</div>

---

> [!CAUTION]
> **Active Development**: Validibot is under active development and not yet ready for production use. APIs, database schemas, and features may change without notice. We welcome early adopters and contributors, but please be aware that breaking changes are expected until we reach v1.0.
>
> Watch the repo or [join the discussion](https://github.com/danielmcquillen/validibot/discussions) to stay updated on progress.

---

## What is Validibot?

Validibot is an **open-source data validation platform** that transforms fragmented validation processes into systematic, reliable workflows. Originally built for validating building energy models (EnergyPlus, FMU), it's designed to handle any structured data validation.

**Key problems Validibot solves:**

- **Complicated manual processes**: Your current data validation involves a number of tools and manual processes
- **Inconsistency**: Different teams implementing different validation logic for similar data
- **Fragmentation**: Validation scattered across codebases, scripts, and manual processes
- **Poor visibility**: No centralized view of validation results, trends, or failures
- **Limited reusability**: Validation logic written once can't easily be shared or reused

## Key Features

### Built-in Validators

Run directly in the Django process—no extra infrastructure needed:

- **JSON Schema**: Validate JSON against JSON Schema drafts 4-2020-12
- **XML Schema (XSD)**: Validate XML against W3C XML Schema
- **Basic Assertions**: Flexible field validation with CEL expressions
- **AI Validation**: Natural language rules powered by LLMs

### Advanced Validators

Run as isolated Docker containers for complex domain-specific validation:

- **EnergyPlus**: Validate IDF and epJSON building energy models
- **FMU (FMI)**: Validate Functional Mock-up Units for simulation
- **Custom**: Bring your own validator containers

### Workflow Engine

Orchestrate multi-step validation pipelines:

- Ordered sequence of validation steps
- Mix built-in and advanced validators
- Action steps for notifications (Slack, webhooks)
- Versioned workflows for safe migrations

### Full REST API

Integrate validation into your existing tools:

```bash
# Submit a file for validation
curl -X POST https://your-instance.com/api/v1/submissions/ \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@model.idf" \
  -F "workflow_id=wf_abc123"
```

See the [API documentation](https://validibot.com/docs/api) for complete reference.

## Quick Start

### Prerequisites

- Docker and Docker Compose (or [Podman](https://podman.io/) for rootless containers)
- 4GB RAM minimum (8GB recommended)

### One-Command Setup

```bash
# Clone the repository
git clone https://github.com/validibot/validibot.git
cd validibot

# Start all services
docker compose up -d

# Create your admin account
docker compose exec web python manage.py createsuperuser
```

Open http://localhost:8000 and log in with your admin credentials.

For detailed setup instructions, see the [Installation Guide](https://validibot.com/docs/installation).

## Architecture

Validibot uses a **two-layer architecture** for maximum flexibility:

```
┌─────────────────────────────────────────────────────────────┐
│                     Validibot Core                          │
├─────────────────────────────────────────────────────────────┤
│  Built-in Validators     │    Advanced Validators          │
│  ────────────────────    │    ────────────────────          │
│  • JSON Schema           │    • EnergyPlus (Docker)        │
│  • XML Schema            │    • FMU/FMI (Docker)           │
│  • Basic Assertions      │    • Custom Containers          │
│  • AI Validation         │                                  │
│  (runs in Django)        │    (isolated containers)        │
├─────────────────────────────────────────────────────────────┤
│  Workflow Engine • REST API • Web UI • Celery Workers      │
├─────────────────────────────────────────────────────────────┤
│  PostgreSQL              │    Redis                        │
└─────────────────────────────────────────────────────────────┘
```

**Built-in validators** run in the Django process for low-latency validation of common formats.

**Advanced validators** run in isolated Docker containers with resource limits, network isolation, and automatic cleanup—perfect for complex domain-specific tools that need their own dependencies.

See [Architecture Overview](https://validibot.com/docs/architecture) for more details.

## Self-Hosted Deployment

Validibot is designed for **self-hosted deployment**. You control your infrastructure, data, and security posture.

### Production Stack

| Component      | Purpose                             |
| -------------- | ----------------------------------- |
| **Web**        | Django application (API + UI)       |
| **Worker**     | Celery workers for async validation |
| **PostgreSQL** | Primary database                    |
| **Redis**      | Task queue broker and cache         |
| **Caddy**      | Reverse proxy with automatic TLS    |

### Deployment Options

- **Docker Compose**: Recommended for most deployments. See [Docker deployment guide](https://validibot.com/docs/deployment/docker).
- **Kubernetes**: Helm chart coming soon. See [Kubernetes guide](https://validibot.com/docs/deployment/kubernetes).
- **Cloud Run (GCP)**: For Google Cloud deployments. See [GCP guide](https://validibot.com/docs/deployment/gcp).

### Security Considerations

> [!IMPORTANT]
> Docker socket access grants root-equivalent privileges on the host. For production deployments, we recommend using [Podman](https://podman.io/) which is rootless by default, or running Docker in rootless mode.

Key security features:

- **Resource limits**: CPU, memory, and timeout limits on all validator containers
- **Network isolation**: Validator containers run with `network_mode='none'`
- **Automatic cleanup**: Orphaned containers are cleaned up via the Ryuk pattern
- **Non-root processes**: Web and worker containers run as non-root users

See the [Security Guide](https://validibot.com/docs/security) for complete recommendations.

## Open-Core Licensing

Validibot follows an **open-core model**. The core platform is free and open-source under AGPL-3.0, with optional commercial extensions for teams that need additional features.

### What's Free (Community Edition)

Everything you need to run a complete validation platform:

- All built-in validators (JSON Schema, XML Schema, Basic, AI)
- All advanced validators (EnergyPlus, FMU, custom containers)
- Unlimited workflows and validation runs
- Full REST API and web interface
- Single-organization workspace
- Basic user roles (Owner, Admin, Author, Executor, Viewer)
- Command-line interface ([validibot-cli](https://github.com/validibot/validibot-cli))

### What's in Pro

For teams integrating validation into CI/CD pipelines:

- Machine-readable outputs (JUnit XML, SARIF, JSON)
- Parallel validation execution
- Incremental validation (only re-run what changed)
- Baseline comparison for findings
- Metrics export for observability tools
- Email support

See [Pricing](https://validibot.com/pricing) for details.

### License Details

| Repository              | License    | Purpose                         |
| ----------------------- | ---------- | ------------------------------- |
| `validibot` (this repo) | AGPL-3.0   | Core platform                   |
| `validibot-validators`  | MIT        | Advanced validator containers   |
| `validibot-cli`         | MIT        | Command-line interface          |
| `validibot-shared`      | MIT        | Shared library for integrations |
| `validibot-pro`         | Commercial | Pro tier features               |

The AGPL-3.0 license requires that if you modify Validibot and provide it as a network service, you must make your modifications available under the same license. For commercial use without this requirement, [contact us](mailto:sales@validibot.com) for a commercial license.

## Documentation

| Resource                                                      | Description                      |
| ------------------------------------------------------------- | -------------------------------- |
| [Getting Started](https://validibot.com/docs/getting-started) | First steps with Validibot       |
| [Installation Guide](https://validibot.com/docs/installation) | Detailed deployment instructions |
| [User Guide](https://validibot.com/docs/user-guide)           | How to use the platform          |
| [API Reference](https://validibot.com/docs/api)               | REST API documentation           |
| [Developer Docs](https://validibot.com/docs/developers)       | Contributing and architecture    |
| [CLI Documentation](https://validibot.com/docs/cli)           | Command-line interface usage     |

## Support

### Community Support

- **GitHub Discussions**: [Ask questions and share ideas](https://github.com/danielmcquillen/validibot/discussions)
- **GitHub Issues**: [Report bugs](https://github.com/danielmcquillen/validibot/issues)

> [!NOTE]
> Community support is provided on a best-effort basis by volunteers. For guaranteed response times and priority support, consider [Validibot Pro](https://validibot.com/pricing).

### Commercial Support

Pro and Enterprise customers receive:

- Priority email support
- Guaranteed response times (SLA)
- Direct access to the development team
- Assistance with deployment and integration

[Contact Sales](mailto:sales@validibot.com) to learn more.

## Contributing

We welcome contributions! Whether it's:

- Reporting bugs
- Suggesting features
- Improving documentation
- Submitting pull requests

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

### Development Setup

```bash
# Clone the repo
git clone https://github.com/validibot/validibot.git
cd validibot

# Install dependencies with uv
uv sync --extra dev

# Set up environment
source set-env.sh

# Run tests
uv run pytest

# Run linter
uv run ruff check
```

See the [Developer Guide](https://validibot.com/docs/developers/setup) for complete instructions.

## Roadmap

Track our progress and upcoming features:

- [Public Roadmap](https://github.com/orgs/validibot/projects/1)
- [Changelog](CHANGELOG.md)

## Related Projects

| Project                                                                   | Description                     |
| ------------------------------------------------------------------------- | ------------------------------- |
| [validibot-cli](https://github.com/validibot/validibot-cli)               | Command-line interface          |
| [validibot-validators](https://github.com/validibot/validibot-validators) | Advanced validator containers   |
| [validibot-shared](https://github.com/validibot/validibot-shared)         | Shared library for integrations |

## Acknowledgments

Validibot is built on the shoulders of giants:

- [Django](https://djangoproject.com/) - The web framework
- [Celery](https://docs.celeryq.dev/) - Distributed task queue
- [EnergyPlus](https://energyplus.net/) - Building energy simulation (U.S. Department of Energy)
- [FMPy](https://github.com/CATIA-Systems/FMPy) - FMU simulation library
- [Cookiecutter Django](https://github.com/cookiecutter/cookiecutter-django/) - Project template

## License

Validibot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

```
Copyright (c) 2025-2026 McQuillen Interactive Pty. Ltd.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
```

## Trademarks

The Validibot name, logo, robot character, and associated branding are trademarks of **McQuillen Interactive Pty. Ltd.** and may not be used without permission. This trademark policy does not limit your rights under the AGPL-3.0 license to use, modify, and distribute the software.

For trademark usage guidelines, contact [hello@validibot.com](mailto:hello@validibot.com).

---

<div align="center">

[Website](https://validibot.com) •
[Docs](https://validibot.com/docs) •
[Community](https://github.com/validibot/validibot/discussions) •
[Contact](mailto:hello@validibot.com)

</div>

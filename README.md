<div align="center">

<picture>
  <img src="https://validibot.com/static/marketing/images/robot_scene_schema_validation.png" alt="Validibot - Data Validation Robot" width="420" style="border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.12); margin: 24px 0;">
</picture>

# Validibot

**Open-source data validation engine for energy models, simulations, and beyond**

[![Build Status](https://github.com/danielmcquillen/validibot/actions/workflows/ci.yml/badge.svg)](https://github.com/danielmcquillen/validibot/actions)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
[![Django 5.2](https://img.shields.io/badge/django-5.2-green.svg)](https://djangoproject.com/)

[User Documentation](https://docs.validibot.com/) •
[Developer Documentation](https://dev.validibot.com/) •
[Getting Started](https://docs.validibot.com/getting-started) •
[Community](https://github.com/danielmcquillen/validibot/discussions) •
[Pricing](https://validibot.com/pricing)

</div>

## Related Projects

Validibot is composed of several repositories that work together:

| Repository                                                                          | Description                                                                                            | License  |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | -------- |
| **[validibot](https://github.com/danielmcquillen/validibot)**                       | Core platform (this repo) — Django web application, REST API, workflow engine, and built-in validators | AGPL-3.0 |
| **[validibot-cli](https://github.com/danielmcquillen/validibot-cli)**               | Command-line interface for running validations from terminals and CI/CD pipelines                      | MIT      |
| **[validibot-validators](https://github.com/danielmcquillen/validibot-validators)** | Advanced validators (EnergyPlus, FMI) that run as isolated Docker containers                           | MIT      |
| **[validibot-shared](https://github.com/danielmcquillen/validibot-shared)**         | Shared Pydantic models defining the data interchange format between core and validators                | MIT      |

---

## What is Validibot?

Validibot is an **open-source data validation platform** that transforms fragmented validation processes into systematic, reliable validation workflows. Originally built for validating building energy models (using EnergyPlus), it's now designed to handle any structured data validation with complex logic or simulations (e.g. an arbitrary FMU file).

**Key problems Validibot solves:**

- **Complicated manual processes**: Your current data validation involves a number of tools and manual processes
- **Inconsistency**: Different teams implementing different validation logic for similar data
- **Fragmentation**: Validation scattered across codebases, scripts, and manual processes
- **Poor visibility**: No centralized view of validation results, trends, or failures
- **Limited reusability**: Validation logic written once can't easily be shared or reused

## Key Features

### "Simple" Validators

These validators run directly in a Django "worker" process -- no extra infrastructure needed:
(Note : validators are at various stages of development):

- **JSON Schema**: Validate JSON against JSON Schema drafts 4-2020-12
- **XML Schema (XSD)**: Validate XML against W3C XML Schema
- **Basic Assertions**: Flexible field validation with CEL expressions
- **AI Validation**: Natural language rules powered by LLMs (coming soon...)

### "Advanced" Validators

These validators run as isolated Docker containers for complex domain-specific validation:

- **EnergyPlus**: Validate IDF and epJSON building energy models
- **FMU (FMI)**: Validate Functional Mock-up Units via OpenModelica simulation
- **Custom**: Bring your own validator containers

Validibot defines a simple container interface for advanced validators: read an input envelope, perform validation, write an output envelope. This makes it straightforward to package any validation logic as a container. See the [Container Interface Guide](https://dev.validibot.com/overview/validator_architecture/) for the full specification.

### Workflow Engine

Orchestrate multi-step validation pipelines:

- Ordered sequence of validation steps
- Mix simple and advanced validators
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

See the [API documentation](https://docs.validibot.com/api) for complete reference.

(And check out the **[validibot-cli](https://github.com/danielmcquillen/validibot-cli)** for a simple way to access the API...)

## Quick Start

### Prerequisites

- Docker and Docker Compose (or [Podman](https://podman.io/) for rootless containers)
- 4GB RAM minimum (8GB recommended)

### One-Command Setup

```bash
# Clone the repository
git clone https://github.com/danielmcquillen/validibot.git
cd validibot

# Start all services
docker compose up -d

# Create your admin account
docker compose exec web python manage.py createsuperuser
```

Open http://localhost:8000 and log in with your admin credentials.

For detailed setup instructions, see the [Installation Guide](https://docs.validibot.com/installation).

## Deployment

Validibot is designed for **deployment on your own infrastructure**. You control your infrastructure, data, and security posture.

### Production Stack

| Component         | Purpose                                                   |
| ----------------- | --------------------------------------------------------- |
| **Web**           | Django application (API + UI)                             |
| **Worker**        | Celery workers for async validation                       |
| **PostgreSQL**    | Primary database                                          |
| **Redis**         | Task queue broker and cache                               |
| **Reverse Proxy** | User-provided (Caddy, Traefik, nginx) for TLS termination |

### Deployment Options

- **Docker Compose**: Recommended for most deployments. See [Docker deployment guide](https://docs.validibot.com/deployment/docker).
- **Google Cloud Run (GCP)**: For Google Cloud deployments. (Guide coming soon...).
- **AWS**: (planned...)
- **Kubernetes**: (planned...)

### Reverse Proxy

Validibot doesn't include a reverse proxy by default. You'll need to set up your own for TLS termination. We recommend **[Caddy](https://caddyserver.com/)** for its automatic HTTPS with zero configuration.

See the [Reverse Proxy Guide](https://dev.validibot.com/deployment/reverse-proxy/) for setup instructions, including examples for nginx, Traefik, and Cloudflare Tunnel.

### Security Considerations

> [!IMPORTANT]
> Docker socket access grants root-equivalent privileges on the host. For production deployments, we recommend using [Podman](https://podman.io/) which is rootless by default, or running Docker in rootless mode.

> [!WARNING]
> **Only run advanced validator containers that you have built and control yourself.** Never run third-party or untrusted container images as validators—they execute with access to your validation data and could potentially compromise your system.

Key security features:

- **Resource limits**: CPU, memory, and timeout limits on all validator containers
- **Network isolation**: Validator containers run with `network_mode='none'`
- **Automatic cleanup**: Orphaned containers are cleaned up via the Ryuk pattern
- **Non-root processes**: Web and worker containers run as non-root users

See the [Security Guide](https://docs.validibot.com/security) for complete recommendations.

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
- Command-line interface ([validibot-cli](https://github.com/danielmcquillen/validibot-cli))

### What's in Pro

For teams that need more:

- Multiple workspaces per organization
- Team management with workspace-level roles
- Advanced analytics and reporting
- Cryptographically signed validation badges (JWKS)
- Billing and subscription management
- Priority email support

See [Pricing](https://validibot.com/pricing) for details. Need something else? [Get in touch](mailto:sales@mcquilleninteractive.com).

### License Details

| Repository              | License    | Purpose                         |
| ----------------------- | ---------- | ------------------------------- |
| `validibot` (this repo) | AGPL-3.0   | Core platform                   |
| `validibot-validators`  | MIT        | Advanced validator containers   |
| `validibot-cli`         | MIT        | Command-line interface          |
| `validibot-shared`      | MIT        | Shared library for integrations |
| `validibot-pro`         | Commercial | Pro tier features               |

The AGPL-3.0 license requires that if you modify Validibot and provide it as a network service, you must make your modifications available under the same license. For commercial use without this requirement, [contact us](mailto:sales@mcquilleninteractive.com) for a commercial license.

## Documentation

| Resource                                                      | Description                      |
| ------------------------------------------------------------- | -------------------------------- |
| [Getting Started](https://docs.validibot.com/getting-started) | First steps with Validibot       |
| [Installation Guide](https://docs.validibot.com/installation) | Detailed deployment instructions |
| [User Guide](https://docs.validibot.com/user-guide)           | How to use the platform          |
| [API Reference](https://docs.validibot.com/api)               | REST API documentation           |
| [Developer Docs](https://dev.validibot.com/)                  | Contributing and architecture    |
| [CLI Documentation](https://docs.validibot.com/cli)           | Command-line interface usage     |

## Support

### Community Support

- **GitHub Discussions**: [Ask questions and share ideas](https://github.com/danielmcquillen/validibot/discussions)
- **GitHub Issues**: [Report bugs](https://github.com/danielmcquillen/validibot/issues)

> [!NOTE]
> Community support is provided on a best-effort basis (by me). For guaranteed response times and priority support, consider [Validibot Pro](https://validibot.com/pricing).

### Commercial Support

Pro and Enterprise customers receive:

- Priority email support
- Guaranteed response times (SLA)
- Direct access to the development team
- Assistance with deployment and integration

[Contact Sales](mailto:sales@mcquilleninteractive.com) to learn more.

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
git clone https://github.com/danielmcquillen/validibot.git
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

See the [Developer Guide](https://dev.validibot.com/setup) for complete instructions.

## Roadmap

Track our progress and upcoming features:

- [GitHub Issues & Milestones](https://github.com/danielmcquillen/validibot/milestones)
- [Changelog](CHANGELOG.md)

## Acknowledgments

Validibot is built on a number of open-source software projects, including:

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

For trademark usage guidelines, contact [hello@mcquilleninteractive.com](mailto:hello@mcquilleninteractive.com).

---

<div align="center">

[Website](https://validibot.com) •
[Docs](https://docs.validibot.com) •
[Community](https://github.com/danielmcquillen/validibot/discussions) •
[Contact](mailto:hello@mcquilleninteractive.com)

</div>

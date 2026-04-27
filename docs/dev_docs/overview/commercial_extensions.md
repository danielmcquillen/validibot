# Commercial Extensions

Validibot follows an open-core model. The core application is open source under AGPL-3.0 and includes the full validation system, all built-in validators, workflows, and single-user management. Two optional commercial packages add team and enterprise capabilities:

- **validibot-pro** -- team management, billing, advanced analytics, signed credentials, MCP server
- **validibot-enterprise** -- multi-org support, SSO/SAML, LDAP integration (includes all Pro features)

!!! note "MCP is community code, Pro-licensed"
    The MCP (Model Context Protocol) server is a special case worth
    flagging up front: its source, Dockerfile, and deploy recipes
    live in this repo under `mcp/`, `compose/production/mcp/Dockerfile`,
    and `just/mcp/mod.just`. A self-hoster can build and run the
    container without any commercial package installed. However, at
    startup the MCP server calls
    `GET /api/v1/license/features/` against the Django API and
    refuses to serve traffic unless the `mcp_server` feature is
    advertised — which only happens when validibot-pro (or
    enterprise) is installed. The code is community for portability
    and clarity; the commercial boundary is enforced at runtime.

If you want the lower-level extension mechanics, read [Plugin Architecture](plugin_architecture.md) alongside this page. That document explains the shared registry and sync pattern used by both validators and actions.

## How commercial packages plug in

Commercial packages are standard Python packages distributed through a private package index. Installing the package is the license -- there are no runtime license keys.

To activate a commercial package, the customer does two things:

1. Install the package into the Python environment or Docker image that runs Validibot.
2. Add the package's Django app to `INSTALLED_APPS`.

This is an explicit opt-in. It keeps activation visible in settings and supports commercial packages that ship models, migrations, templates, static files, or other normal Django app behavior.

## Installing a commercial package

### Host-managed Python environment

Install the package into the same Python environment that runs Validibot (see your license email for the index URL and credentials):

```bash
uv pip install --python .venv/bin/python --index <private-index-url> validibot-pro==<version>
```

Then add the Django app in `config/settings/base.py`:

```python
INSTALLED_APPS += ["validibot_pro"]
```

Restart the application after updating settings.

### Docker-based self-hosting

For Docker-based installs, bake the package into the image using the optional `.build` file:

```bash
cp .envs.example/.production/.docker-compose/.build .envs/.production/.docker-compose/.build
```

Then set:

```bash
VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro==<version>
VALIDIBOT_PRIVATE_INDEX_URL=https://<license-credentials>@pypi.validibot.com/simple/
```

`VALIDIBOT_COMMERCIAL_PACKAGE` must be an exact package reference. Use either
an exact version like `validibot-pro==0.1.0` together with
`VALIDIBOT_PRIVATE_INDEX_URL=https://<license-credentials>@pypi.validibot.com/simple/`,
or a quoted exact wheel URL on `pypi.validibot.com` such as
`"https://<license-credentials>@pypi.validibot.com/packages/validibot_pro-0.1.0-py3-none-any.whl#sha256=<hash>"`.
Floating names like `validibot-pro` are intentionally rejected during Docker
builds.

Installing the wheel into the image is only the first step. Add the Django app
in `config/settings/base.py` before you rebuild:

```python
INSTALLED_APPS += ["validibot_pro"]
```

For Enterprise, use `validibot-enterprise` instead and add both Django apps:

```python
INSTALLED_APPS += ["validibot_pro", "validibot_enterprise"]
```

After that, rebuild with `just docker-compose bootstrap` on first install or `just docker-compose deploy` for later rebuilds.

## What you'll see in the codebase

As you browse the core codebase, you'll encounter two patterns that reference commercial features:

**Feature flags in templates.** Some navigation links and UI elements are wrapped in `{% if feature_team_management %}` or similar checks. These elements are hidden when the corresponding commercial package is not installed.

**Feature guard mixins on views.** Some views include `FeatureRequiredMixin` with a `required_commercial_feature` attribute. These views return a 404 when the feature is not in the active license's feature set. This is defense-in-depth alongside the template-level hiding.

Both patterns read from the active license. `validibot/core/features.py` defines `CommercialFeature` — an enum of every feature name commercial packages can activate — and exposes `is_feature_enabled()` and a template-context processor (`get_feature_context()`) that builds the `feature_<name>: bool` keys templates check against.

## Editions and the license module

Everything commercial flows through a single `License` object defined in `validibot/core/license.py`. A License carries two pieces of information:

- `edition` — the tier (Community, Pro, Enterprise) for coarse-grained checks.
- `features` — a `frozenset[str]` of the exact feature names this license unlocks, for fine-grained `is_feature_enabled(...)` checks.

Commercial packages build their License object once (e.g. `validibot_pro/license.py` declares `PRO_LICENSE`) and call `set_license(PRO_LICENSE)` at import time. Community code never imports commercial packages — it only calls `get_license()` / `is_feature_enabled()`.

When Enterprise is also installed, it loads after Pro (Django INSTALLED_APPS order) and overwrites the whole License with its own declaration, which unions Pro's features with Enterprise-only additions. Last-writer-wins keeps the model simple.

```python
from validibot.core.license import get_license, Edition

lic = get_license()
if lic.edition.includes(Edition.PRO):
    # Pro or Enterprise
    ...
```

For most cases, the feature flag approach (`is_feature_enabled()`) is preferred over edition checks because it's more granular — a feature can migrate between tiers without touching the gating code.

## How commercial code plugs into workflows

Commercial packages do not need community code to host every concrete action or view. The shared contracts live in the community repo, but the commercial package can provide the actual implementation.

Today that means:

- community owns the `License` type, the `CommercialFeature` enum, the workflow orchestration, and the action registry contract
- `validibot-pro` declares its `License` object (listing exactly the features Pro provides) and calls `set_license(PRO_LICENSE)` at import time, then registers Pro-owned workflow actions from its own `AppConfig.ready()`
- synced `ActionDefinition` rows make those actions appear in the step picker just like built-in actions

This keeps the open-core boundary cleaner. The community app knows how to host plugins, while Pro and Enterprise packages own the commercial behavior itself.

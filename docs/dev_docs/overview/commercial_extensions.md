# Commercial Extensions

Validibot follows an open-core model. The core application is open source under AGPL-3.0 and includes the full validation system, all built-in validators, workflows, and single-user management. Two optional commercial packages add team and enterprise capabilities:

- **validibot-pro** -- team management, billing, advanced analytics, signed credentials
- **validibot-enterprise** -- multi-org support, SSO/SAML, LDAP integration (includes all Pro features)

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
uv pip install --python .venv/bin/python --index <private-index-url> validibot-pro
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
VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro
VALIDIBOT_PRIVATE_INDEX_URL=https://<license-credentials>@pypi.validibot.com/simple/
```

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

**Feature guard mixins on views.** Some views include `FeatureRequiredMixin` with a `required_feature` attribute. These views return a 404 when the feature is not registered. This is defense-in-depth alongside the template-level hiding.

Both patterns use the feature registry in `validibot/core/features.py`, which defines `CommercialFeature` -- an enum of all features that commercial packages can activate.

## Editions and the license module

The `validibot/core/license.py` module provides edition detection. The three editions (Community, Pro, Enterprise) form a hierarchy where higher tiers include all lower-tier capabilities. Commercial packages register a license provider at startup, and `get_license()` returns the current edition.

Code that needs to check the edition directly (rather than a specific feature flag) can use:

```python
from validibot.core.license import get_license, Edition

license = get_license()
if license.edition.includes(Edition.PRO):
    # Pro or Enterprise
    ...
```

For most cases, the feature flag approach (`is_feature_enabled()`) is preferred over edition checks because it's more granular.

# Commercial Extensions

Validibot follows an open-core model. The core application is open source under AGPL-3.0 and includes the full validation engine, all built-in validators, workflows, and single-user management. Two optional commercial packages add team and enterprise capabilities:

- **validibot-pro** -- team management, billing, advanced analytics, signed badges
- **validibot-enterprise** -- multi-org support, SSO/SAML, LDAP integration (includes all Pro features)

## How commercial packages plug in

Commercial packages are standard Python packages distributed through a private package index. Installing the package is the license -- there are no runtime license keys.

At startup, Django discovers commercial packages via Python entry points. Each package registers a `validibot.plugins` entry point that calls `register_feature()` for the features it provides. The core application never imports commercial code directly; it only checks whether features have been registered.

```
# In validibot-pro's pyproject.toml
[project.entry-points."validibot.plugins"]
pro = "validibot_pro.registration"
```

The core app discovers these entry points in its `AppConfig.ready()` method and calls the registration module automatically.

## Installing a commercial package

Add the package from the private index (see your license email for the index URL and credentials):

```bash
uv add validibot-pro --index <private-index-url>
```

Then restart the application. The Pro features will be available immediately.

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

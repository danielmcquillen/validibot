# License Enforcement

Validibot uses an open-core model with three editions: **Community** (AGPL-3.0), **Pro** (commercial), and **Enterprise** (commercial). This guide explains how edition enforcement works and how to integrate it into your code.

## Overview

The license module (`validibot.core.license`) provides:

- **Edition detection**: Community, Pro, or Enterprise
- **Edition gating**: Restricts code paths based on edition
- **License provider registration**: Allows commercial packages to unlock editions

## Philosophy

Validibot follows a simple licensing model
: installing a commercial package activates the license. There's no runtime license key validation - the private package index authentication \*is\* the license enforcement. If you can install `validibot-pro` or `validibot-enterprise`, you have a valid license.

The Community edition gives users full access to all built-in validators. Pro adds multi-organization support, advanced team management, and priority support. Enterprise adds SSO/LDAP integration, guest management, and source code escrow.

## Edition Hierarchy

Editions follow a tier system where higher editions include all capabilities of lower editions:

```
Enterprise (tier 2) > Pro (tier 1) > Community (tier 0)
```

- **Enterprise** includes all Pro and Community capabilities
- **Pro** includes all Community capabilities
- **Community** is the base edition

## Using the License Module

### Checking the Current Edition

```python
from validibot.core.license import get_license, Edition

license = get_license()

if license.is_enterprise:
    # Enterprise-only code path
    ...
elif license.is_pro:
    # Pro code path (also includes Enterprise)
    ...
elif license.is_community:
    # Community code path
    ...

# Or use is_commercial to check for any commercial license
if license.is_commercial:
    # Pro or Enterprise
    ...
```

### Requiring Minimum Editions

Use the `require_edition` decorator when you need a minimum edition level:

```python
from validibot.core.license import require_edition, Edition

@require_edition(Edition.PRO, "Multi-organization support")
def create_organization():
    """Create a new organization. Requires Pro or Enterprise."""
    ...

@require_edition(Edition.ENTERPRISE, "LDAP integration")
def configure_ldap():
    """Configure LDAP. Requires Enterprise."""
    ...
```

### Manual Edition Checks

Check manually and raise an error:

```python
from validibot.core.license import get_license, Edition, LicenseError

def handle_organization_request(action: str, data):
    license = get_license()

    if action == "create":
        license.require_edition(Edition.PRO, "Multi-organization support")
        return create_organization(data)
    elif action == "list":
        return list_organizations(data)  # Always available
```

### Checking Edition Availability

Use `is_edition_available()` for conditional logic without raising errors:

```python
from validibot.core.license import is_edition_available, Edition

if is_edition_available(Edition.PRO):
    show_organization_menu()
else:
    show_upgrade_prompt()
```

## Commercial License Integration

### How It Works

Commercial packages (`validibot-pro`, `validibot-enterprise`) are distributed via a private package index. The authentication required to access this index serves as the license enforcement - if you can install the package, you have paid for it.

When a commercial package is installed and imported, it registers its license provider with the core system. No license key or environment variable is required for activation.

### Pro License

The `validibot-pro` package registers itself on import:

```python
# In validibot_pro/__init__.py
from validibot_pro.license_provider import register_pro_license

# Auto-register when package is imported
register_pro_license()
```

The license provider simply returns a valid Pro license:

```python
# In validibot_pro/license_provider.py
from validibot.core.license import register_license_provider, License, Edition

def get_pro_license():
    """Return a Pro license. Package installation = license activation."""
    return License(
        edition=Edition.PRO,
        organization=os.environ.get("VALIDIBOT_PRO_ORGANIZATION", "Pro License"),
    )

def register_pro_license():
    register_license_provider(Edition.PRO, get_pro_license)
```

### Enterprise License

The `validibot-enterprise` package works identically:

```python
# In validibot_enterprise/license_provider.py
from validibot.core.license import register_license_provider, License, Edition

def get_enterprise_license():
    """Return an Enterprise license. Package installation = license activation."""
    return License(
        edition=Edition.ENTERPRISE,
        organization=os.environ.get("VALIDIBOT_ENTERPRISE_ORGANIZATION", "Enterprise License"),
    )

def register_enterprise_license():
    register_license_provider(Edition.ENTERPRISE, get_enterprise_license)
```

### Provider Precedence

When both Pro and Enterprise providers are registered, Enterprise takes precedence. The `get_license()` function checks providers in tier order (highest first).

### Plugin Auto-Discovery

Commercial packages register themselves as entry points:

```toml
# pyproject.toml for validibot-pro
[project.entry-points."validibot.plugins"]
pro = "validibot_pro:register_pro_license"
```

Validibot automatically discovers and loads these plugins at startup via `importlib.metadata.entry_points()`.

### Installation

Users install commercial editions from the private package index:

```bash
# Pro
pip install validibot-pro --index-url https://<credentials>@packages.validibot.com/simple/

# Enterprise
pip install validibot-enterprise --index-url https://<credentials>@packages.validibot.com/simple/
```

The package is automatically discovered and activated - no configuration required.

Optionally, users can customize the organization name displayed in logs:

```bash
export VALIDIBOT_PRO_ORGANIZATION="Acme Corp"
# or
export VALIDIBOT_ENTERPRISE_ORGANIZATION="Enterprise Customer"
```

## Error Handling

License errors include the required edition and helpful upgrade information:

```python
from validibot.core.license import LicenseError

try:
    create_organization(data)
except LicenseError as e:
    # e.feature_description contains the description
    # e.required_edition contains the minimum required edition
    print(str(e))
    # "Multi-organization support requires Validibot Pro.
    #  The Community edition includes all validators.
    #  Pro adds additional capabilities for teams.
    #  Learn more: https://validibot.com/pricing"
```

## Testing

When writing tests, use the license provider functions to simulate different editions:

```python
import pytest
from validibot.core.license import (
    register_license_provider,
    reset_license_provider,
    get_license,
    License,
    Edition,
)

@pytest.fixture
def pro_license():
    """Fixture to enable Pro license for tests."""
    def provider():
        return License(edition=Edition.PRO)

    register_license_provider(Edition.PRO, provider)
    yield
    reset_license_provider()

@pytest.fixture
def enterprise_license():
    """Fixture to enable Enterprise license for tests."""
    def provider():
        return License(edition=Edition.ENTERPRISE)

    register_license_provider(Edition.ENTERPRISE, provider)
    yield
    reset_license_provider()

def test_create_org_pro_only(pro_license):
    """Organization creation should work with Pro license."""
    result = create_organization({"name": "Test Org"})
    assert result is not None

def test_ldap_enterprise_only(enterprise_license):
    """LDAP integration should work with Enterprise license."""
    result = configure_ldap()
    assert result is not None
```

## Best Practices

1. **Gate at the entry point**: Check edition at API endpoints or CLI commands, not deep in business logic.

2. **Provide clear feedback**: Always tell users what requires which edition and how to upgrade.

3. **Graceful degradation**: When possible, fall back to Community-compatible alternatives rather than failing.

4. **Don't over-gate**: Core validation functionality should always work. Only gate organization/team management capabilities.

5. **Test all editions**: Ensure code works correctly for Community, Pro, and Enterprise users.

6. **Use descriptive messages**: Pass a clear `feature_description` to `require_edition()` so error messages are helpful.

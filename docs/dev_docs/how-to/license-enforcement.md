# License Enforcement

Validibot uses an open-core model with three editions: **Community** (AGPL-3.0), **Pro** (commercial), and **Enterprise** (commercial). This guide explains how edition enforcement works and how to integrate it into your code.

## Overview

The license module (`validibot.core.license`) provides:

- **Edition detection**: Community, Pro, or Enterprise
- **CI environment detection**: Blocks Community edition in CI/CD
- **Edition gating**: Restricts code paths based on edition
- **License provider registration**: Allows commercial packages to unlock editions

## Philosophy

> Community is for humans. Pro is for machines. Enterprise is for organizations.

The Community edition gives users full access to all validators for local/interactive use. Pro adds automation features for CI/CD pipelines and machine integrations. Enterprise adds multi-organization support, SSO, and other features for larger organizations.

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

@require_edition(Edition.PRO, "JUnit XML output")
def generate_junit_report():
    """Generate JUnit XML report. Requires Pro or Enterprise."""
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

def generate_report(format: str, results):
    license = get_license()

    if format == "junit":
        license.require_edition(Edition.PRO, "JUnit XML output")
        return generate_junit(results)
    elif format == "text":
        return generate_text(results)  # Always available
```

### CI Environment Detection

The module automatically detects CI environments and raises `CIEnvironmentError` for Community edition:

```python
from validibot.core.license import check_ci_allowed, CIEnvironmentError

try:
    check_ci_allowed()
except CIEnvironmentError as e:
    print(f"Cannot run in {e.ci_name} with Community edition")
    print("Upgrade to Pro: https://validibot.com/pricing")
```

Detected CI environments include:

- GitHub Actions
- GitLab CI
- Jenkins
- CircleCI
- Travis CI
- Azure Pipelines
- Bitbucket Pipelines
- AWS CodeBuild
- Google Cloud Build
- Buildkite
- TeamCity
- Drone CI
- And more (via generic `CI=true` detection)

## Commercial License Integration

### Pro License

The `validibot-pro` package registers itself on import:

```python
# In validibot_pro/__init__.py
from validibot.core.license import register_license_provider, License, Edition

def _get_pro_license():
    """Load and validate Pro license."""
    # Check license file, environment variable, etc.
    if valid_license:
        return License(
            edition=Edition.PRO,
            organization="Customer Org",
        )
    return None

# Register on import
register_license_provider(Edition.PRO, _get_pro_license)
```

### Enterprise License

The `validibot-enterprise` package works similarly:

```python
# In validibot_enterprise/__init__.py
from validibot.core.license import register_license_provider, License, Edition

def _get_enterprise_license():
    """Load and validate Enterprise license."""
    # Check license file, environment variable, etc.
    if valid_license:
        return License(
            edition=Edition.ENTERPRISE,
            organization="Enterprise Customer Org",
        )
    return None

# Register on import
register_license_provider(Edition.ENTERPRISE, _get_enterprise_license)
```

### Provider Precedence

When both Pro and Enterprise providers are registered, Enterprise takes precedence. The `get_license()` function checks providers in tier order (highest first).

### Installation

Users install commercial editions with:

```bash
# Pro
pip install validibot-pro --index-url https://<credentials>@packages.validibot.com/simple/

# Enterprise
pip install validibot-enterprise --index-url https://<credentials>@packages.validibot.com/simple/
```

Then add to Django settings:

```python
# config/settings/base.py
INSTALLED_APPS = [
    ...
    "validibot_enterprise",  # Or "validibot_pro" - must be before validibot apps
]
```

## Error Handling

License errors include the required edition and helpful upgrade information:

```python
from validibot.core.license import LicenseError, CIEnvironmentError

try:
    generate_junit_report(results)
except LicenseError as e:
    # e.feature_description contains the description
    # e.required_edition contains the minimum required edition
    print(str(e))
    # "JUnit XML output requires Validibot Pro.
    #  The Community edition includes all validators for local/interactive use.
    #  Pro adds additional capabilities for teams and automation.
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

def test_junit_output_pro_only(pro_license):
    """JUnit output should work with Pro license."""
    result = generate_junit_report(test_results)
    assert "<testsuites>" in result

def test_ldap_enterprise_only(enterprise_license):
    """LDAP integration should work with Enterprise license."""
    result = configure_ldap()
    assert result is not None
```

## Best Practices

1. **Gate at the entry point**: Check edition at API endpoints or CLI commands, not deep in business logic.

2. **Provide clear feedback**: Always tell users what requires which edition and how to upgrade.

3. **Graceful degradation**: When possible, fall back to Community-compatible alternatives rather than failing.

4. **Don't over-gate**: Core validation functionality should always work. Only gate automation/integration capabilities.

5. **Test all editions**: Ensure code works correctly for Community, Pro, and Enterprise users.

6. **Use descriptive messages**: Pass a clear `feature_description` to `require_edition()` so error messages are helpful.

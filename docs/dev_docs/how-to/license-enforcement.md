# License Enforcement

Validibot uses an open-core model with two editions: **Community** (AGPL-3.0) and **Pro** (commercial). This guide explains how license enforcement works and how to integrate it into your code.

## Overview

The license module (`validibot.core.license`) provides:

- **Edition detection**: Community vs Pro
- **CI environment detection**: Blocks Community edition in CI/CD
- **Feature gating**: Restricts Pro features in Community edition
- **Pro provider registration**: Allows validibot-pro package to unlock features

## Philosophy

> Community is for humans. Pro is for machines.

The Community edition gives users full access to all validators for local/interactive use. The Pro edition adds automation features for CI/CD pipelines and machine integrations.

## Feature Differences

| Feature Category | Community | Pro |
|-----------------|-----------|-----|
| All validators | ✓ | ✓ |
| CLI usage | ✓ | ✓ |
| Local/interactive use | ✓ | ✓ |
| CI/CD environments | ✗ | ✓ |
| API access | ✗ | ✓ |
| Machine-readable outputs (JUnit, SARIF, JSON) | ✗ | ✓ |
| Rich reports (HTML, PDF) | ✗ | ✓ |
| Parallel execution | ✗ | ✓ |
| Incremental validation | ✗ | ✓ |
| Baseline comparison | ✗ | ✓ |
| Metrics export | ✗ | ✓ |

See [editions.md](/docs/user_docs/editions.md) for the complete feature comparison.

## Using the License Module

### Checking the Current Edition

```python
from validibot.core.license import get_license, Edition

license = get_license()

if license.is_pro:
    # Pro-only code path
    ...
elif license.is_community:
    # Community code path
    ...
```

### Checking for Specific Features

```python
from validibot.core.license import is_feature_available, ProFeature

if is_feature_available(ProFeature.OUTPUT_JUNIT):
    # Generate JUnit output
    ...
else:
    # Fall back to text output
    ...
```

### Requiring Pro Features

Use the `require_pro` decorator for functions that require Pro:

```python
from validibot.core.license import require_pro, ProFeature

@require_pro(ProFeature.OUTPUT_JUNIT)
def generate_junit_report(results):
    """Generate JUnit XML report. Pro only."""
    ...
```

Or check manually and raise an error:

```python
from validibot.core.license import get_license, ProFeature, LicenseError

def generate_report(format: str, results):
    license = get_license()

    if format == "junit":
        license.require_feature(ProFeature.OUTPUT_JUNIT)
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

## Pro License Integration

The `validibot-pro` package registers itself on import:

```python
# In validibot_pro/__init__.py
from validibot.core.license import register_pro_license_provider, License, Edition, ProFeature

def _get_pro_license():
    """Load and validate Pro license."""
    # Check license file, environment variable, etc.
    if valid_license:
        return License(
            edition=Edition.PRO,
            organization="Customer Org",
            features=frozenset(ProFeature),  # All features
        )
    return None

# Register on import
register_pro_license_provider(_get_pro_license)
```

Users install Pro with:

```bash
pip install validibot-pro --index-url https://<credentials>@packages.validibot.com/simple/
```

Then add to Django settings:

```python
# config/settings/base.py
INSTALLED_APPS = [
    ...
    "validibot_pro",  # Must be before validibot apps
]
```

## Available Pro Features

The `ProFeature` enum defines all gated features:

```python
class ProFeature(str, Enum):
    # Environment
    CI_CD_EXECUTION = "ci_cd_execution"
    API_ACCESS = "api_access"

    # Output formats
    OUTPUT_JUNIT = "output_junit"
    OUTPUT_SARIF = "output_sarif"
    OUTPUT_JSON = "output_json"
    OUTPUT_HTML_REPORT = "output_html_report"
    OUTPUT_PDF_REPORT = "output_pdf_report"

    # Performance
    PARALLEL_EXECUTION = "parallel_execution"
    INCREMENTAL_VALIDATION = "incremental_validation"

    # Workflow
    BASELINE_COMPARISON = "baseline_comparison"
    CONFIGURABLE_EXIT_CODES = "configurable_exit_codes"
    PR_COMMENT_INTEGRATION = "pr_comment_integration"

    # Observability
    METRICS_EXPORT = "metrics_export"
```

## Error Handling

License errors include helpful upgrade information:

```python
from validibot.core.license import LicenseError, CIEnvironmentError

try:
    generate_junit_report(results)
except LicenseError as e:
    # e.feature contains the feature enum/string
    print(str(e))
    # "JUnit XML output requires Validibot Pro.
    #  The Community edition includes all validators for local/interactive use.
    #  Pro adds CI/CD integration, machine-readable outputs, and more.
    #  Learn more: https://validibot.com/pricing"
```

## Testing

When writing tests, use the `register_pro_license_provider` function to simulate Pro:

```python
import pytest
from validibot.core.license import (
    register_pro_license_provider,
    get_license,
    License,
    Edition,
)

@pytest.fixture
def pro_license():
    """Fixture to enable Pro license for tests."""
    def provider():
        return License(edition=Edition.PRO)

    register_pro_license_provider(provider)
    yield

    # Clean up
    import validibot.core.license as license_module
    license_module._pro_license_provider = None
    get_license.cache_clear()

def test_junit_output_pro_only(pro_license):
    """JUnit output should work with Pro license."""
    result = generate_junit_report(test_results)
    assert "<testsuites>" in result
```

## Best Practices

1. **Gate at the entry point**: Check license at API endpoints or CLI commands, not deep in business logic.

2. **Provide clear feedback**: Always tell users what feature requires Pro and how to upgrade.

3. **Graceful degradation**: When possible, fall back to Community-compatible alternatives rather than failing.

4. **Don't over-gate**: Core validation functionality should always work. Only gate automation/integration features.

5. **Test both editions**: Ensure code works correctly for both Community and Pro users.

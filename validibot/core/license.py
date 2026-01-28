"""
License detection and feature gating for Validibot editions.

Validibot is available in two editions:

- **Community** (AGPL-3.0): Full validation capabilities for local/interactive use
- **Pro** (Commercial): Adds CI/CD integration, machine-readable outputs, etc.

This module detects the current edition and enforces feature restrictions.
The Pro edition is provided by the `validibot-pro` package which, when installed,
registers itself and unlocks Pro features.

CI Environment Detection
------------------------
The Community edition blocks execution in CI/CD environments. This is how we keep
the project sustainable while keeping the core validation engine free.

Pro Feature Checks
------------------
Pro features include:
- CI/CD environment execution
- API access
- Machine-readable outputs (JUnit XML, SARIF, JSON)
- Rich reports (HTML, PDF)
- Parallel execution
- Incremental validation
- Baseline comparison
- Metrics export

Usage::

    from validibot.core.license import get_license, Edition, require_pro

    lic = get_license()
    if lic.edition == Edition.PRO:
        # Pro-only code path
        ...

    # Or use the decorator/context manager
    @require_pro("JUnit XML output")
    def generate_junit_report():
        ...
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from functools import lru_cache
from typing import TYPE_CHECKING
from typing import TypeVar

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

logger = logging.getLogger(__name__)

# Type variable for decorator
F = TypeVar("F", bound=Callable)


class Edition(str, Enum):
    """Validibot edition identifier."""

    COMMUNITY = "community"
    PRO = "pro"


class ProFeature(str, Enum):
    """
    Pro-only features that require a commercial license.

    These features are blocked in the Community edition and require
    the validibot-pro package to be installed.
    """

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


# Human-readable names for Pro features (used in error messages)
PRO_FEATURE_NAMES: dict[ProFeature, str] = {
    ProFeature.CI_CD_EXECUTION: "CI/CD environment execution",
    ProFeature.API_ACCESS: "API access",
    ProFeature.OUTPUT_JUNIT: "JUnit XML output",
    ProFeature.OUTPUT_SARIF: "SARIF output",
    ProFeature.OUTPUT_JSON: "JSON output",
    ProFeature.OUTPUT_HTML_REPORT: "HTML reports",
    ProFeature.OUTPUT_PDF_REPORT: "PDF reports",
    ProFeature.PARALLEL_EXECUTION: "Parallel execution",
    ProFeature.INCREMENTAL_VALIDATION: "Incremental validation",
    ProFeature.BASELINE_COMPARISON: "Baseline comparison",
    ProFeature.CONFIGURABLE_EXIT_CODES: "Configurable exit codes",
    ProFeature.PR_COMMENT_INTEGRATION: "PR/MR comment integration",
    ProFeature.METRICS_EXPORT: "Metrics export",
}


class LicenseError(Exception):
    """Raised when a Pro feature is used without a Pro license."""

    def __init__(self, feature: ProFeature | str, message: str | None = None):
        if isinstance(feature, ProFeature):
            feature_name = PRO_FEATURE_NAMES.get(feature, feature.value)
        else:
            feature_name = feature

        if message is None:
            message = (
                f"{feature_name} requires Validibot Pro.\n\n"
                "The Community edition includes all validators "
                "for local/interactive use.\n"
                "Pro adds CI/CD integration, machine-readable outputs, and more.\n\n"
                "Learn more: https://validibot.com/pricing"
            )
        super().__init__(message)
        self.feature = feature


class CIEnvironmentError(LicenseError):
    """Raised when Community edition is run in a CI/CD environment."""

    def __init__(self, ci_name: str):
        message = (
            "Validibot Community edition cannot run in CI/CD environments "
            f"({ci_name}).\n\n"
            "The Community edition is for local/interactive use only.\n"
            "For CI/CD integration, you need Validibot Pro.\n\n"
            "Learn more: https://validibot.com/pricing"
        )
        super().__init__(ProFeature.CI_CD_EXECUTION, message)
        self.ci_name = ci_name


@dataclass
class License:
    """
    Represents the current Validibot license.

    Attributes:
        edition: The edition (Community or Pro)
        features: Set of enabled Pro features (empty for Community)
        organization: Organization name for Pro licenses
    """

    edition: Edition
    features: AbstractSet[ProFeature] = field(default_factory=frozenset)
    organization: str | None = None

    @property
    def is_pro(self) -> bool:
        """Check if this is a Pro license."""
        return self.edition == Edition.PRO

    @property
    def is_community(self) -> bool:
        """Check if this is a Community license."""
        return self.edition == Edition.COMMUNITY

    def has_feature(self, feature: ProFeature) -> bool:
        """Check if a specific Pro feature is available."""
        if self.edition == Edition.PRO:
            # Pro has all features unless explicitly restricted
            return feature in self.features or not self.features
        return False

    def require_feature(self, feature: ProFeature) -> None:
        """
        Require a Pro feature, raising LicenseError if not available.

        Args:
            feature: The feature to require

        Raises:
            LicenseError: If the feature is not available
        """
        if not self.has_feature(feature):
            raise LicenseError(feature)


# CI environment detection patterns
# Each tuple is (env_var, value_pattern, ci_name)
# If value_pattern is None, just check if the env var exists
CI_ENVIRONMENT_PATTERNS: list[tuple[str, str | None, str]] = [
    # GitHub Actions
    ("GITHUB_ACTIONS", "true", "GitHub Actions"),
    # GitLab CI
    ("GITLAB_CI", "true", "GitLab CI"),
    # Jenkins
    ("JENKINS_URL", None, "Jenkins"),
    ("BUILD_ID", None, "Jenkins"),  # Older Jenkins
    # CircleCI
    ("CIRCLECI", "true", "CircleCI"),
    # Travis CI
    ("TRAVIS", "true", "Travis CI"),
    # Azure Pipelines
    ("TF_BUILD", "True", "Azure Pipelines"),
    ("AZURE_PIPELINES", None, "Azure Pipelines"),
    # Bitbucket Pipelines
    ("BITBUCKET_BUILD_NUMBER", None, "Bitbucket Pipelines"),
    # AWS CodeBuild
    ("CODEBUILD_BUILD_ID", None, "AWS CodeBuild"),
    # Google Cloud Build
    ("BUILDER_OUTPUT", None, "Google Cloud Build"),
    # Buildkite
    ("BUILDKITE", "true", "Buildkite"),
    # TeamCity
    ("TEAMCITY_VERSION", None, "TeamCity"),
    # Drone CI
    ("DRONE", "true", "Drone CI"),
    # Woodpecker CI
    ("CI_PIPELINE_ID", None, "Woodpecker CI"),
    # Semaphore
    ("SEMAPHORE", "true", "Semaphore"),
    # AppVeyor
    ("APPVEYOR", "True", "AppVeyor"),
    # Harness CI
    ("HARNESS_BUILD_ID", None, "Harness CI"),
    # Generic CI indicator (many CI systems set this)
    ("CI", "true", "CI environment"),
]


def detect_ci_environment() -> str | None:
    """
    Detect if running in a CI/CD environment.

    Returns:
        The name of the detected CI environment, or None if not in CI.
    """
    for env_var, value_pattern, ci_name in CI_ENVIRONMENT_PATTERNS:
        env_value = os.environ.get(env_var)
        if env_value is not None:
            if value_pattern is None:
                # Just check existence
                return ci_name
            if env_value.lower() == value_pattern.lower():
                return ci_name
    return None


def is_ci_environment() -> bool:
    """Check if running in a CI/CD environment."""
    return detect_ci_environment() is not None


# Registry for Pro license providers
# The validibot-pro package registers itself here on import
_pro_license_provider: Callable[[], License | None] | None = None


def register_pro_license_provider(provider: Callable[[], License | None]) -> None:
    """
    Register a Pro license provider.

    This is called by the validibot-pro package when it's imported.
    The provider function should return a License object if a valid
    Pro license is available, or None otherwise.

    Args:
        provider: A callable that returns a License or None
    """
    global _pro_license_provider  # noqa: PLW0603
    _pro_license_provider = provider
    # Clear the cached license so it gets re-evaluated
    get_license.cache_clear()
    logger.info("Pro license provider registered")


def reset_license_provider() -> None:
    """
    Reset the license provider (for testing).

    This clears any registered Pro provider and the license cache.
    """
    global _pro_license_provider  # noqa: PLW0603
    _pro_license_provider = None
    get_license.cache_clear()


@lru_cache(maxsize=1)
def get_license() -> License:
    """
    Get the current Validibot license.

    This function is cached - the license is determined once per process.
    Call `get_license.cache_clear()` to force re-evaluation.

    Returns:
        The current License object
    """
    # Check if Pro license provider is registered
    if _pro_license_provider is not None:
        pro_license = _pro_license_provider()
        if pro_license is not None:
            logger.debug("Using Pro license: %s", pro_license.organization)
            return pro_license

    # Default to Community edition
    return License(edition=Edition.COMMUNITY)


def check_ci_allowed() -> None:
    """
    Check if CI/CD execution is allowed under current license.

    Raises:
        CIEnvironmentError: If in CI and using Community edition
    """
    ci_name = detect_ci_environment()
    if ci_name is not None:
        lic = get_license()
        if lic.is_community:
            raise CIEnvironmentError(ci_name)


def require_pro(feature: ProFeature | str) -> Callable[[F], F]:
    """
    Decorator to require a Pro feature.

    Args:
        feature: The Pro feature required, or a string description

    Returns:
        Decorator that raises LicenseError if feature is not available

    Example::

        @require_pro(ProFeature.OUTPUT_JUNIT)
        def generate_junit_report():
            ...

        @require_pro("Custom reporting")
        def custom_feature():
            ...
    """

    def decorator(func: F) -> F:
        def wrapper(*args, **kwargs):
            lic = get_license()
            if isinstance(feature, ProFeature):
                lic.require_feature(feature)
            elif lic.is_community:
                raise LicenseError(feature)
            return func(*args, **kwargs)

        # Preserve function metadata
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper  # type: ignore[return-value]

    return decorator


def is_feature_available(feature: ProFeature) -> bool:
    """
    Check if a Pro feature is available under current license.

    This is a convenience function for conditional code paths.

    Args:
        feature: The feature to check

    Returns:
        True if the feature is available, False otherwise
    """
    return get_license().has_feature(feature)

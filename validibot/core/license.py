"""
License detection and edition gating for Validibot.

Validibot is available in three editions:

- **Community** (AGPL-3.0): Full validation capabilities for local/interactive use
- **Pro** (Commercial): Adds CI/CD integration, machine-readable outputs, etc.
- **Enterprise** (Commercial): Adds multi-org, LDAP, distributed execution, custom SLAs

This module detects the current edition and enforces edition restrictions.
Commercial editions are provided by separate packages:
- `validibot-pro` - unlocks Pro edition
- `validibot-enterprise` - unlocks Enterprise edition (includes all Pro capabilities)

For more on Validibot editions, see: https://validibot.com/pricing

"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import TypeVar

from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

# Type variable for decorator
F = TypeVar("F", bound=Callable)


class Edition(str, Enum):
    """Validibot edition identifier."""

    COMMUNITY = "community"
    PRO = "pro"
    ENTERPRISE = "enterprise"

    @property
    def tier(self) -> int:
        """
        Return the tier level for comparison.

        Higher tier = more features. Enterprise > Pro > Community.
        """
        return {
            Edition.COMMUNITY: 0,
            Edition.PRO: 1,
            Edition.ENTERPRISE: 2,
        }[self]

    def includes(self, other: Edition) -> bool:
        """
        Check if this edition includes capabilities of another edition.

        Enterprise includes Pro and Community capabilities.
        Pro includes Community capabilities.
        """
        return self.tier >= other.tier


class LicenseError(Exception):
    """Raised when a feature is used without the required license."""

    def __init__(
        self,
        feature_description: str,
        required_edition: Edition = Edition.PRO,
        message: str | None = None,
    ):
        edition_name = required_edition.value.title()

        if message is None:
            message = _(
                "%(feature_description)s requires Validibot %(edition_name)s.\n\n"
                "The Community edition includes all validators "
                "for local/interactive use.\n"
                "%(edition_name)s adds additional capabilities for teams and "
                "automation.\n\nLearn more: https://validibot.com/pricing"
            ) % {
                "feature_description": feature_description,
                "edition_name": edition_name,
            }
        super().__init__(message)
        self.feature_description = feature_description
        self.required_edition = required_edition


class CIEnvironmentError(LicenseError):
    """Raised when Community edition is run in a CI/CD environment."""

    def __init__(self, ci_name: str):
        message = _(
            "Validibot Community edition cannot run in CI/CD environments "
            "(%(ci_name)s).\n\n"
            "The Community edition is for local/interactive use only.\n"
            "For CI/CD integration, you need Validibot Pro.\n\n"
            "Learn more: https://validibot.com/pricing"
        ) % {"ci_name": ci_name}
        super().__init__(_("CI/CD execution"), Edition.PRO, message)
        self.ci_name = ci_name


@dataclass
class License:
    """
    Represents the current Validibot license.

    Attributes:
        edition: The edition (Community, Pro, or Enterprise)
        organization: Organization name for commercial licenses
    """

    edition: Edition
    organization: str | None = None

    @property
    def is_pro(self) -> bool:
        """Check if this is at least a Pro license (Pro or Enterprise)."""
        return self.edition.includes(Edition.PRO)

    @property
    def is_enterprise(self) -> bool:
        """Check if this is an Enterprise license."""
        return self.edition == Edition.ENTERPRISE

    @property
    def is_community(self) -> bool:
        """Check if this is a Community license."""
        return self.edition == Edition.COMMUNITY

    @property
    def is_commercial(self) -> bool:
        """Check if this is a commercial license (Pro or Enterprise)."""
        return self.edition != Edition.COMMUNITY

    def require_edition(
        self,
        edition: Edition,
        feature_description: str = "",
    ) -> None:
        """
        Require a minimum edition, raising LicenseError if not met.

        Args:
            edition: The minimum edition required
            feature_description: Human-readable description of what requires
                this edition

        Raises:
            LicenseError: If current edition doesn't meet requirement
        """
        if not self.edition.includes(edition):
            if not feature_description:
                feature_description = _("This feature")
            raise LicenseError(feature_description, required_edition=edition)


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


# Registry for license providers
# Commercial packages register themselves here on import
# Enterprise takes precedence over Pro
_license_providers: dict[Edition, Callable[[], License | None]] = {}


def register_license_provider(
    edition: Edition,
    provider: Callable[[], License | None],
) -> None:
    """
    Register a license provider for an edition.

    This is called by commercial packages when they're imported:
    - validibot-pro registers for Edition.PRO
    - validibot-enterprise registers for Edition.ENTERPRISE

    Args:
        edition: The edition this provider handles
        provider: A callable that returns a License or None
    """
    _license_providers[edition] = provider
    # Clear the cached license so it gets re-evaluated
    get_license.cache_clear()
    logger.info("%s license provider registered", edition.value.title())


def reset_license_provider() -> None:
    """
    Reset all license providers (for testing).

    This clears any registered providers and the license cache.
    """
    _license_providers.clear()
    get_license.cache_clear()


@lru_cache(maxsize=1)
def get_license() -> License:
    """
    Get the current Validibot license.

    Checks license providers in order of precedence:
    1. Enterprise (highest tier)
    2. Pro
    3. Falls back to Community

    This function is cached - the license is determined once per process.
    Call `get_license.cache_clear()` to force re-evaluation.

    Returns:
        The current License object
    """
    # Check providers in order of tier (highest first)
    for edition in [Edition.ENTERPRISE, Edition.PRO]:
        provider = _license_providers.get(edition)
        if provider is not None:
            lic = provider()
            if lic is not None:
                logger.debug(
                    "Using %s license: %s",
                    lic.edition.value.title(),
                    lic.organization,
                )
                return lic

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


def require_edition(
    edition: Edition,
    feature_description: str = "",
) -> Callable[[F], F]:
    """
    Decorator to require a minimum edition.

    Args:
        edition: The minimum edition required (PRO or ENTERPRISE)
        feature_description: Human-readable description of what requires this edition

    Returns:
        Decorator that raises LicenseError if edition requirement not met

    Example::

        @require_edition(Edition.PRO, "JUnit XML output")
        def generate_junit_report():
            ...

        @require_edition(Edition.ENTERPRISE, "LDAP integration")
        def ldap_integration():
            ...
    """

    def decorator(func: F) -> F:
        def wrapper(*args, **kwargs):
            lic = get_license()
            desc = feature_description or func.__name__
            lic.require_edition(edition, desc)
            return func(*args, **kwargs)

        # Preserve function metadata
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper  # type: ignore[return-value]

    return decorator


def is_edition_available(edition: Edition) -> bool:
    """
    Check if an edition level is available under current license.

    Args:
        edition: The edition to check

    Returns:
        True if current license meets or exceeds the edition, False otherwise
    """
    return get_license().edition.includes(edition)

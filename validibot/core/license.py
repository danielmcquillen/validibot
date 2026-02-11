"""
License detection and edition gating for Validibot.

Validibot is available in three editions:

- **Community** (AGPL-3.0): Full validation capabilities, all validators included
- **Pro** (Commercial): Adds multi-org support, removes AGPL obligations
- **Enterprise** (Commercial): Adds SSO/LDAP, guest management, source code escrow

This module detects the current edition based on installed packages.
Commercial editions are provided by separate packages:
- `validibot-pro` - unlocks Pro edition
- `validibot-enterprise` - unlocks Enterprise edition (includes all Pro capabilities)

License enforcement follows a simple model: installing the package activates
the edition. The private package index authentication is the license enforcement.

For more on Validibot editions, see: https://validibot.com/pricing

"""

from __future__ import annotations

import logging
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
                "The Community edition includes all validators.\n"
                "%(edition_name)s adds additional capabilities for teams.\n\n"
                "Learn more: https://validibot.com/pricing"
            ) % {
                "feature_description": feature_description,
                "edition_name": edition_name,
            }
        super().__init__(message)
        self.feature_description = feature_description
        self.required_edition = required_edition


@dataclass
class License:
    """
    Represents the current Validibot license.

    Attributes:
        edition: The edition (Community, Pro, or Enterprise)
        organization: Organization name for commercial licenses (optional)
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

        @require_edition(Edition.PRO, "Multi-organization support")
        def create_organization():
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

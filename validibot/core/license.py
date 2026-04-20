"""
License detection and edition gating for Validibot.

Validibot is available in three editions:

- **Community** (AGPL-3.0): Full validation capabilities, all validators included
- **Pro** (Commercial): Adds team management, guest access, and signed credentials
- **Enterprise** (Commercial): Adds multi-org management, SSO/SAML, and
  enterprise support

This module detects the current edition based on installed packages.
Commercial editions are provided by separate packages:

- ``validibot-pro`` — unlocks Pro edition
- ``validibot-enterprise`` — unlocks Enterprise edition (includes all Pro capabilities)

License enforcement follows a simple model: installing the package activates
the edition. The private package index authentication is the license
enforcement.

### Mechanism

Commercial packages call :func:`set_license` at import time with a
pre-built :class:`License` instance that carries both the edition and
the feature set that edition unlocks. The community code never imports
commercial packages — it only reads ``get_license()`` to branch on
tier, and ``is_feature_enabled()`` (from :mod:`validibot.core.features`)
to gate specific capabilities.

Because the License object carries its own features, there is no
separate feature registry — a single call sets both the tier and
exactly the features that tier provides.

For more on Validibot editions, see https://validibot.com/pricing
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from typing import TypeVar

from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

# Type variable for decorator
F = TypeVar("F", bound=Callable)


class Edition(StrEnum):
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


@dataclass(frozen=True)
class License:
    """
    Represents the current Validibot license.

    Carries two pieces of information:

    - ``edition`` — the tier (Community / Pro / Enterprise) for
      coarse-grained checks like ``is_pro``.
    - ``features`` — the exact set of commercial feature names this
      license unlocks, for fine-grained ``is_feature_enabled(...)``
      checks.

    A License is a value object: construct it once at import time in
    the commercial package, hand it to :func:`set_license`. There is
    no mutation in place.
    """

    edition: Edition
    features: frozenset[str] = field(default_factory=frozenset)

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


# Module-level current license. Defaults to Community — commercial
# packages overwrite it at import time via :func:`set_license`.
#
# Higher-tier packages register LAST in Django's INSTALLED_APPS order
# (Enterprise after Pro), so the final value reflects the highest
# tier available. This ordering is an explicit invariant — see
# ``AppConfig`` ordering in commercial packages.
_COMMUNITY_LICENSE: License = License(edition=Edition.COMMUNITY)
_current_license: License = _COMMUNITY_LICENSE


def set_license(lic: License) -> None:
    """Set the current Validibot license.

    Called by commercial packages at import time (via their
    ``AppConfig.ready()`` or ``__init__.py``). Later calls overwrite
    earlier ones — higher-tier packages loading later win naturally.

    The License object is frozen, so after this call every
    ``get_license()`` reader sees a consistent snapshot without
    defensive copying.
    """
    global _current_license  # noqa: PLW0603 — intentional module-level state
    _current_license = lic
    logger.debug("Using %s license", lic.edition.value.title())


def get_license() -> License:
    """Return the current Validibot license.

    Defaults to Community. Commercial packages call
    :func:`set_license` to replace it at import time.
    """
    return _current_license


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

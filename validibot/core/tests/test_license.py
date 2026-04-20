"""
Tests for the license module.

Covers the Edition enum, the frozen ``License`` dataclass, the
``set_license`` / ``get_license`` pair, and the
``require_edition`` decorator.
"""

from __future__ import annotations

import pytest

from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import LicenseError
from validibot.core.license import get_license
from validibot.core.license import is_edition_available
from validibot.core.license import require_edition
from validibot.core.license import set_license


class TestEdition:
    """Tests for the Edition enum."""

    def test_edition_values(self):
        """Edition enum should have expected values."""
        assert Edition.COMMUNITY.value == "community"
        assert Edition.PRO.value == "pro"
        assert Edition.ENTERPRISE.value == "enterprise"

    def test_edition_tier(self):
        """Edition tiers should be ordered correctly."""
        assert Edition.COMMUNITY.tier < Edition.PRO.tier
        assert Edition.PRO.tier < Edition.ENTERPRISE.tier

    def test_edition_includes(self):
        """Edition.includes should check tier hierarchy."""
        # Community only includes itself
        assert Edition.COMMUNITY.includes(Edition.COMMUNITY) is True
        assert Edition.COMMUNITY.includes(Edition.PRO) is False
        assert Edition.COMMUNITY.includes(Edition.ENTERPRISE) is False

        # Pro includes Community and itself
        assert Edition.PRO.includes(Edition.COMMUNITY) is True
        assert Edition.PRO.includes(Edition.PRO) is True
        assert Edition.PRO.includes(Edition.ENTERPRISE) is False

        # Enterprise includes all
        assert Edition.ENTERPRISE.includes(Edition.COMMUNITY) is True
        assert Edition.ENTERPRISE.includes(Edition.PRO) is True
        assert Edition.ENTERPRISE.includes(Edition.ENTERPRISE) is True


class TestLicense:
    """Tests for the License dataclass.

    License is frozen — construction validates shape, property
    accessors cover the tier-membership helpers, and features
    default to the empty frozenset.
    """

    def test_community_license(self):
        """Community license has no features by default."""
        lic = License(edition=Edition.COMMUNITY)

        assert lic.edition == Edition.COMMUNITY
        assert lic.is_community is True
        assert lic.is_pro is False
        assert lic.is_enterprise is False
        assert lic.is_commercial is False
        assert lic.features == frozenset()

    def test_pro_license_with_features(self):
        """Pro license carries its feature set inline."""
        lic = License(
            edition=Edition.PRO,
            features=frozenset({"team_management", "billing"}),
        )

        assert lic.edition == Edition.PRO
        assert lic.is_community is False
        assert lic.is_pro is True
        assert lic.is_enterprise is False
        assert lic.is_commercial is True
        assert "team_management" in lic.features

    def test_enterprise_license(self):
        """Enterprise is a Pro superset, both flags True."""
        lic = License(edition=Edition.ENTERPRISE)

        assert lic.edition == Edition.ENTERPRISE
        assert lic.is_community is False
        assert lic.is_pro is True  # Enterprise includes Pro
        assert lic.is_enterprise is True
        assert lic.is_commercial is True

    def test_require_edition_community(self):
        """Community should not satisfy Pro or Enterprise requirements."""
        lic = License(edition=Edition.COMMUNITY)

        with pytest.raises(LicenseError) as exc_info:
            lic.require_edition(Edition.PRO, "Signed credentials")

        assert "Signed credentials" in str(exc_info.value)
        assert "Validibot Pro" in str(exc_info.value)

    def test_require_edition_pro_for_enterprise(self):
        """Pro should not satisfy Enterprise requirement."""
        lic = License(edition=Edition.PRO)

        with pytest.raises(LicenseError) as exc_info:
            lic.require_edition(Edition.ENTERPRISE, "LDAP integration")

        assert "LDAP integration" in str(exc_info.value)
        assert "Enterprise" in str(exc_info.value)

    def test_require_edition_enterprise(self):
        """Enterprise should satisfy all requirements."""
        lic = License(edition=Edition.ENTERPRISE)

        # Should not raise
        lic.require_edition(Edition.PRO, "Signed credentials")
        lic.require_edition(Edition.ENTERPRISE, "LDAP integration")

    def test_require_edition_default_description(self):
        """require_edition should use default description if none provided."""
        lic = License(edition=Edition.COMMUNITY)

        with pytest.raises(LicenseError) as exc_info:
            lic.require_edition(Edition.PRO)

        assert "This feature" in str(exc_info.value)


class TestLicenseError:
    """Tests for license error classes."""

    def test_license_error_with_description(self):
        """LicenseError should include feature description and upgrade info."""
        error = LicenseError("Signed credentials")

        assert "Signed credentials" in str(error)
        assert "Validibot Pro" in str(error)
        assert "validibot.com/pricing" in str(error)

    def test_license_error_with_enterprise(self):
        """LicenseError for Enterprise should mention Enterprise."""
        error = LicenseError("LDAP integration", required_edition=Edition.ENTERPRISE)

        assert "LDAP integration" in str(error)
        assert "Enterprise" in str(error)


class TestGetLicense:
    """Tests for ``set_license`` / ``get_license`` and default behaviour.

    The root conftest autouse fixture snapshots and restores the
    license around every test, so these tests can ``set_license``
    freely without manual teardown.
    """

    def test_get_license_default_community(self):
        """Before any set_license, get_license returns Community."""
        # Explicitly force the baseline for this test — the conftest
        # fixture's snapshot captures whatever the environment set
        # at test start, which may or may not be Community depending
        # on whether Pro is installed.
        set_license(License(edition=Edition.COMMUNITY))

        lic = get_license()

        assert lic.edition == Edition.COMMUNITY

    def test_set_license_overrides(self):
        """Calling set_license swaps the current license in place."""
        set_license(
            License(
                edition=Edition.PRO,
                features=frozenset({"team_management"}),
            ),
        )

        lic = get_license()
        assert lic.edition == Edition.PRO
        assert "team_management" in lic.features

    def test_later_set_license_wins(self):
        """set_license has last-writer-wins semantics — the pattern
        Enterprise uses to overwrite Pro's license.
        """
        set_license(License(edition=Edition.PRO))
        set_license(License(edition=Edition.ENTERPRISE))

        assert get_license().edition == Edition.ENTERPRISE


class TestRequireEditionDecorator:
    """Tests for the require_edition decorator."""

    def test_require_edition_pro_community(self):
        """Should raise for Community when Pro required."""
        set_license(License(edition=Edition.COMMUNITY))

        @require_edition(Edition.PRO, "Pro feature")
        def pro_feature():
            return "pro"

        with pytest.raises(LicenseError):
            pro_feature()

    def test_require_edition_pro_pro(self):
        """Should work for Pro when Pro required."""
        set_license(License(edition=Edition.PRO))

        @require_edition(Edition.PRO, "Pro feature")
        def pro_feature():
            return "pro"

        result = pro_feature()
        assert result == "pro"

    def test_require_edition_enterprise(self):
        """Should work for Enterprise when Enterprise required."""
        set_license(License(edition=Edition.ENTERPRISE))

        @require_edition(Edition.ENTERPRISE, "Enterprise feature")
        def enterprise_feature():
            return "enterprise"

        result = enterprise_feature()
        assert result == "enterprise"

    def test_require_edition_enterprise_with_pro(self):
        """Pro should not satisfy Enterprise requirement."""
        set_license(License(edition=Edition.PRO))

        @require_edition(Edition.ENTERPRISE, "Enterprise feature")
        def enterprise_feature():
            return "enterprise"

        with pytest.raises(LicenseError):
            enterprise_feature()

    def test_require_edition_uses_function_name(self):
        """Should use function name as default description."""
        set_license(License(edition=Edition.COMMUNITY))

        @require_edition(Edition.PRO)
        def my_pro_feature():
            return "pro"

        with pytest.raises(LicenseError) as exc_info:
            my_pro_feature()

        assert "my_pro_feature" in str(exc_info.value)

    def test_require_edition_preserves_metadata(self):
        """Decorator should preserve function name and docstring."""

        @require_edition(Edition.PRO, "Pro feature")
        def my_function():
            """My docstring."""
            return "result"

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."


class TestIsEditionAvailable:
    """Tests for is_edition_available helper."""

    def test_community_edition(self):
        """Community should only satisfy Community check."""
        set_license(License(edition=Edition.COMMUNITY))

        assert is_edition_available(Edition.COMMUNITY) is True
        assert is_edition_available(Edition.PRO) is False
        assert is_edition_available(Edition.ENTERPRISE) is False

    def test_pro_edition(self):
        """Pro should satisfy Community and Pro checks."""
        set_license(License(edition=Edition.PRO))

        assert is_edition_available(Edition.COMMUNITY) is True
        assert is_edition_available(Edition.PRO) is True
        assert is_edition_available(Edition.ENTERPRISE) is False

    def test_enterprise_edition(self):
        """Enterprise should satisfy all edition checks."""
        set_license(License(edition=Edition.ENTERPRISE))

        assert is_edition_available(Edition.COMMUNITY) is True
        assert is_edition_available(Edition.PRO) is True
        assert is_edition_available(Edition.ENTERPRISE) is True

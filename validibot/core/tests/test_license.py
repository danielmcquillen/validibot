"""
Tests for the license module.

Tests CI environment detection and edition gating.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from validibot.core.license import CI_ENVIRONMENT_PATTERNS
from validibot.core.license import CIEnvironmentError
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import LicenseError
from validibot.core.license import check_ci_allowed
from validibot.core.license import detect_ci_environment
from validibot.core.license import get_license
from validibot.core.license import is_ci_environment
from validibot.core.license import is_edition_available
from validibot.core.license import register_license_provider
from validibot.core.license import require_edition
from validibot.core.license import reset_license_provider


class TestCIEnvironmentDetection:
    """Tests for CI environment detection."""

    def test_detect_ci_environment_github_actions(self):
        """Should detect GitHub Actions."""
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
            assert detect_ci_environment() == "GitHub Actions"

    def test_detect_ci_environment_gitlab_ci(self):
        """Should detect GitLab CI."""
        with patch.dict(os.environ, {"GITLAB_CI": "true"}, clear=True):
            assert detect_ci_environment() == "GitLab CI"

    def test_detect_ci_environment_jenkins(self):
        """Should detect Jenkins via JENKINS_URL."""
        env = {"JENKINS_URL": "http://jenkins.example.com"}
        with patch.dict(os.environ, env, clear=True):
            assert detect_ci_environment() == "Jenkins"

    def test_detect_ci_environment_jenkins_build_id(self):
        """Should detect Jenkins via BUILD_ID."""
        with patch.dict(os.environ, {"BUILD_ID": "123"}, clear=True):
            assert detect_ci_environment() == "Jenkins"

    def test_detect_ci_environment_circleci(self):
        """Should detect CircleCI."""
        with patch.dict(os.environ, {"CIRCLECI": "true"}, clear=True):
            assert detect_ci_environment() == "CircleCI"

    def test_detect_ci_environment_travis(self):
        """Should detect Travis CI."""
        with patch.dict(os.environ, {"TRAVIS": "true"}, clear=True):
            assert detect_ci_environment() == "Travis CI"

    def test_detect_ci_environment_azure_pipelines(self):
        """Should detect Azure Pipelines."""
        with patch.dict(os.environ, {"TF_BUILD": "True"}, clear=True):
            assert detect_ci_environment() == "Azure Pipelines"

    def test_detect_ci_environment_generic_ci(self):
        """Should detect generic CI=true environment variable."""
        with patch.dict(os.environ, {"CI": "true"}, clear=True):
            assert detect_ci_environment() == "CI environment"

    def test_detect_ci_environment_case_insensitive(self):
        """Should be case-insensitive for value matching."""
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "TRUE"}, clear=True):
            assert detect_ci_environment() == "GitHub Actions"

        with patch.dict(os.environ, {"CI": "True"}, clear=True):
            assert detect_ci_environment() == "CI environment"

    def test_detect_ci_environment_not_in_ci(self):
        """Should return None when not in CI."""
        with patch.dict(os.environ, {}, clear=True):
            assert detect_ci_environment() is None

    def test_detect_ci_environment_wrong_value(self):
        """Should not detect CI if env var has wrong value."""
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}, clear=True):
            # Check that GITHUB_ACTIONS with "false" is not detected
            result = detect_ci_environment()
            # May still detect other patterns if present
            assert result != "GitHub Actions" or result is None

    def test_is_ci_environment(self):
        """Should return boolean for CI detection."""
        with patch.dict(os.environ, {"CI": "true"}, clear=True):
            assert is_ci_environment() is True

        with patch.dict(os.environ, {}, clear=True):
            assert is_ci_environment() is False

    def test_all_ci_patterns_have_name(self):
        """All CI patterns should have a human-readable name."""
        for env_var, _value_pattern, ci_name in CI_ENVIRONMENT_PATTERNS:
            assert ci_name, f"CI pattern {env_var} missing name"
            assert isinstance(ci_name, str)


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
    """Tests for the License dataclass."""

    def test_community_license(self):
        """Community license should have correct properties."""
        lic = License(edition=Edition.COMMUNITY)

        assert lic.edition == Edition.COMMUNITY
        assert lic.is_community is True
        assert lic.is_pro is False
        assert lic.is_enterprise is False
        assert lic.is_commercial is False
        assert lic.organization is None

    def test_pro_license(self):
        """Pro license should have correct properties."""
        lic = License(
            edition=Edition.PRO,
            organization="Acme Corp",
        )

        assert lic.edition == Edition.PRO
        assert lic.is_community is False
        assert lic.is_pro is True
        assert lic.is_enterprise is False
        assert lic.is_commercial is True
        assert lic.organization == "Acme Corp"

    def test_enterprise_license(self):
        """Enterprise license should have correct properties."""
        lic = License(
            edition=Edition.ENTERPRISE,
            organization="Big Corp",
        )

        assert lic.edition == Edition.ENTERPRISE
        assert lic.is_community is False
        assert lic.is_pro is True  # Enterprise includes Pro
        assert lic.is_enterprise is True
        assert lic.is_commercial is True
        assert lic.organization == "Big Corp"

    def test_require_edition_community(self):
        """Community should not satisfy Pro or Enterprise requirements."""
        lic = License(edition=Edition.COMMUNITY)

        with pytest.raises(LicenseError) as exc_info:
            lic.require_edition(Edition.PRO, "JUnit output")

        assert "JUnit output" in str(exc_info.value)
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
        lic.require_edition(Edition.PRO, "JUnit output")
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
        error = LicenseError("JUnit XML output")

        assert "JUnit XML output" in str(error)
        assert "Validibot Pro" in str(error)
        assert "validibot.com/pricing" in str(error)

    def test_license_error_with_enterprise(self):
        """LicenseError for Enterprise should mention Enterprise."""
        error = LicenseError("LDAP integration", required_edition=Edition.ENTERPRISE)

        assert "LDAP integration" in str(error)
        assert "Enterprise" in str(error)

    def test_ci_environment_error(self):
        """CIEnvironmentError should include CI name and upgrade info."""
        error = CIEnvironmentError("GitHub Actions")

        assert "GitHub Actions" in str(error)
        assert "cannot run in CI/CD" in str(error)
        assert "Validibot Pro" in str(error)
        assert error.ci_name == "GitHub Actions"


class TestGetLicense:
    """Tests for get_license function."""

    def test_get_license_default_community(self):
        """Should return Community edition by default."""
        reset_license_provider()

        lic = get_license()

        assert lic.edition == Edition.COMMUNITY

    def test_get_license_caching(self):
        """get_license should be cached."""
        reset_license_provider()

        license1 = get_license()
        license2 = get_license()

        assert license1 is license2

    def test_register_pro_provider(self):
        """Registering a Pro provider should change license."""
        reset_license_provider()

        def pro_provider():
            return License(edition=Edition.PRO, organization="Test Org")

        try:
            register_license_provider(Edition.PRO, pro_provider)

            lic = get_license()

            assert lic.edition == Edition.PRO
            assert lic.organization == "Test Org"
        finally:
            reset_license_provider()

    def test_register_enterprise_provider(self):
        """Registering an Enterprise provider should change license."""
        reset_license_provider()

        def enterprise_provider():
            return License(edition=Edition.ENTERPRISE, organization="Big Corp")

        try:
            register_license_provider(Edition.ENTERPRISE, enterprise_provider)

            lic = get_license()

            assert lic.edition == Edition.ENTERPRISE
            assert lic.organization == "Big Corp"
        finally:
            reset_license_provider()

    def test_enterprise_takes_precedence_over_pro(self):
        """Enterprise provider should take precedence over Pro."""
        reset_license_provider()

        def pro_provider():
            return License(edition=Edition.PRO, organization="Pro Org")

        def enterprise_provider():
            return License(edition=Edition.ENTERPRISE, organization="Enterprise Org")

        try:
            register_license_provider(Edition.PRO, pro_provider)
            register_license_provider(Edition.ENTERPRISE, enterprise_provider)

            lic = get_license()

            assert lic.edition == Edition.ENTERPRISE
            assert lic.organization == "Enterprise Org"
        finally:
            reset_license_provider()

    def test_provider_returning_none(self):
        """Provider returning None should fall back to Community."""
        reset_license_provider()

        def null_provider():
            return None

        try:
            register_license_provider(Edition.PRO, null_provider)

            lic = get_license()

            assert lic.edition == Edition.COMMUNITY
        finally:
            reset_license_provider()


class TestCheckCIAllowed:
    """Tests for CI check function."""

    def test_check_ci_allowed_not_in_ci(self):
        """Should not raise when not in CI."""
        reset_license_provider()

        with patch.dict(os.environ, {}, clear=True):
            # Should not raise
            check_ci_allowed()

    def test_check_ci_allowed_community_in_ci(self):
        """Should raise when Community edition in CI."""
        reset_license_provider()

        with patch.dict(os.environ, {"CI": "true"}, clear=True):
            with pytest.raises(CIEnvironmentError) as exc_info:
                check_ci_allowed()

            assert exc_info.value.ci_name == "CI environment"

    def test_check_ci_allowed_pro_in_ci(self):
        """Should not raise when Pro edition in CI."""
        reset_license_provider()

        def pro_provider():
            return License(edition=Edition.PRO)

        try:
            register_license_provider(Edition.PRO, pro_provider)

            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
                # Should not raise
                check_ci_allowed()
        finally:
            reset_license_provider()

    def test_check_ci_allowed_enterprise_in_ci(self):
        """Should not raise when Enterprise edition in CI."""
        reset_license_provider()

        def enterprise_provider():
            return License(edition=Edition.ENTERPRISE)

        try:
            register_license_provider(Edition.ENTERPRISE, enterprise_provider)

            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
                # Should not raise
                check_ci_allowed()
        finally:
            reset_license_provider()


class TestRequireEditionDecorator:
    """Tests for the require_edition decorator."""

    def test_require_edition_pro_community(self):
        """Should raise for Community when Pro required."""
        reset_license_provider()

        @require_edition(Edition.PRO, "Pro feature")
        def pro_feature():
            return "pro"

        with pytest.raises(LicenseError):
            pro_feature()

    def test_require_edition_pro_pro(self):
        """Should work for Pro when Pro required."""
        reset_license_provider()

        def pro_provider():
            return License(edition=Edition.PRO)

        @require_edition(Edition.PRO, "Pro feature")
        def pro_feature():
            return "pro"

        try:
            register_license_provider(Edition.PRO, pro_provider)
            result = pro_feature()
            assert result == "pro"
        finally:
            reset_license_provider()

    def test_require_edition_enterprise(self):
        """Should work for Enterprise when Enterprise required."""
        reset_license_provider()

        def enterprise_provider():
            return License(edition=Edition.ENTERPRISE)

        @require_edition(Edition.ENTERPRISE, "Enterprise feature")
        def enterprise_feature():
            return "enterprise"

        try:
            register_license_provider(Edition.ENTERPRISE, enterprise_provider)
            result = enterprise_feature()
            assert result == "enterprise"
        finally:
            reset_license_provider()

    def test_require_edition_enterprise_with_pro(self):
        """Pro should not satisfy Enterprise requirement."""
        reset_license_provider()

        def pro_provider():
            return License(edition=Edition.PRO)

        @require_edition(Edition.ENTERPRISE, "Enterprise feature")
        def enterprise_feature():
            return "enterprise"

        try:
            register_license_provider(Edition.PRO, pro_provider)
            with pytest.raises(LicenseError):
                enterprise_feature()
        finally:
            reset_license_provider()

    def test_require_edition_uses_function_name(self):
        """Should use function name as default description."""
        reset_license_provider()

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
        reset_license_provider()

        assert is_edition_available(Edition.COMMUNITY) is True
        assert is_edition_available(Edition.PRO) is False
        assert is_edition_available(Edition.ENTERPRISE) is False

    def test_pro_edition(self):
        """Pro should satisfy Community and Pro checks."""
        reset_license_provider()

        def pro_provider():
            return License(edition=Edition.PRO)

        try:
            register_license_provider(Edition.PRO, pro_provider)

            assert is_edition_available(Edition.COMMUNITY) is True
            assert is_edition_available(Edition.PRO) is True
            assert is_edition_available(Edition.ENTERPRISE) is False
        finally:
            reset_license_provider()

    def test_enterprise_edition(self):
        """Enterprise should satisfy all edition checks."""
        reset_license_provider()

        def enterprise_provider():
            return License(edition=Edition.ENTERPRISE)

        try:
            register_license_provider(Edition.ENTERPRISE, enterprise_provider)

            assert is_edition_available(Edition.COMMUNITY) is True
            assert is_edition_available(Edition.PRO) is True
            assert is_edition_available(Edition.ENTERPRISE) is True
        finally:
            reset_license_provider()

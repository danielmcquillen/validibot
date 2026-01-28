"""
Tests for the license module.

Tests CI environment detection, edition detection, and Pro feature gating.
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
from validibot.core.license import ProFeature
from validibot.core.license import check_ci_allowed
from validibot.core.license import detect_ci_environment
from validibot.core.license import get_license
from validibot.core.license import is_ci_environment
from validibot.core.license import is_feature_available
from validibot.core.license import register_pro_license_provider
from validibot.core.license import require_pro
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


class TestLicense:
    """Tests for the License dataclass."""

    def test_community_license(self):
        """Community license should have correct properties."""
        lic = License(edition=Edition.COMMUNITY)

        assert lic.edition == Edition.COMMUNITY
        assert lic.is_community is True
        assert lic.is_pro is False
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
        assert lic.organization == "Acme Corp"

    def test_community_has_no_features(self):
        """Community license should not have Pro features."""
        lic = License(edition=Edition.COMMUNITY)

        assert lic.has_feature(ProFeature.CI_CD_EXECUTION) is False
        assert lic.has_feature(ProFeature.OUTPUT_JUNIT) is False
        assert lic.has_feature(ProFeature.PARALLEL_EXECUTION) is False

    def test_pro_has_all_features_by_default(self):
        """Pro license should have all features by default (empty features set)."""
        lic = License(edition=Edition.PRO)

        # Empty features set means all features enabled
        assert lic.has_feature(ProFeature.CI_CD_EXECUTION) is True
        assert lic.has_feature(ProFeature.OUTPUT_JUNIT) is True
        assert lic.has_feature(ProFeature.PARALLEL_EXECUTION) is True

    def test_pro_with_restricted_features(self):
        """Pro license can have restricted feature set."""
        lic = License(
            edition=Edition.PRO,
            features=frozenset({ProFeature.CI_CD_EXECUTION, ProFeature.OUTPUT_JUNIT}),
        )

        assert lic.has_feature(ProFeature.CI_CD_EXECUTION) is True
        assert lic.has_feature(ProFeature.OUTPUT_JUNIT) is True
        assert lic.has_feature(ProFeature.OUTPUT_SARIF) is False

    def test_require_feature_community(self):
        """require_feature should raise for Community edition."""
        lic = License(edition=Edition.COMMUNITY)

        with pytest.raises(LicenseError) as exc_info:
            lic.require_feature(ProFeature.OUTPUT_JUNIT)

        assert "JUnit XML output" in str(exc_info.value)
        assert "Validibot Pro" in str(exc_info.value)

    def test_require_feature_pro(self):
        """require_feature should not raise for Pro edition."""
        lic = License(edition=Edition.PRO)

        # Should not raise
        lic.require_feature(ProFeature.OUTPUT_JUNIT)
        lic.require_feature(ProFeature.PARALLEL_EXECUTION)


class TestLicenseError:
    """Tests for license error classes."""

    def test_license_error_with_feature(self):
        """LicenseError should include feature name and upgrade info."""
        error = LicenseError(ProFeature.OUTPUT_JUNIT)

        assert "JUnit XML output" in str(error)
        assert "Validibot Pro" in str(error)
        assert "validibot.com/pricing" in str(error)

    def test_license_error_with_string(self):
        """LicenseError should accept string feature description."""
        error = LicenseError("Custom reporting")

        assert "Custom reporting" in str(error)
        assert "Validibot Pro" in str(error)

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

        # Define a Pro provider
        def pro_provider():
            return License(edition=Edition.PRO, organization="Test Org")

        try:
            register_pro_license_provider(pro_provider)

            lic = get_license()

            assert lic.edition == Edition.PRO
            assert lic.organization == "Test Org"
        finally:
            reset_license_provider()

    def test_pro_provider_returning_none(self):
        """Provider returning None should fall back to Community."""
        reset_license_provider()

        def null_provider():
            return None

        try:
            register_pro_license_provider(null_provider)

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
            register_pro_license_provider(pro_provider)

            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
                # Should not raise
                check_ci_allowed()
        finally:
            reset_license_provider()


class TestRequireProDecorator:
    """Tests for the require_pro decorator."""

    def test_require_pro_community(self):
        """Decorated function should raise for Community edition."""
        reset_license_provider()

        @require_pro(ProFeature.OUTPUT_JUNIT)
        def generate_junit():
            return "junit output"

        with pytest.raises(LicenseError):
            generate_junit()

    def test_require_pro_pro(self):
        """Decorated function should work for Pro edition."""
        reset_license_provider()

        def pro_provider():
            return License(edition=Edition.PRO)

        @require_pro(ProFeature.OUTPUT_JUNIT)
        def generate_junit():
            return "junit output"

        try:
            register_pro_license_provider(pro_provider)

            result = generate_junit()

            assert result == "junit output"
        finally:
            reset_license_provider()

    def test_require_pro_with_string(self):
        """Decorator should accept string feature description."""
        reset_license_provider()

        @require_pro("Custom feature")
        def custom_feature():
            return "custom"

        with pytest.raises(LicenseError) as exc_info:
            custom_feature()

        assert "Custom feature" in str(exc_info.value)

    def test_require_pro_preserves_metadata(self):
        """Decorator should preserve function name and docstring."""

        @require_pro(ProFeature.OUTPUT_JUNIT)
        def my_function():
            """My docstring."""
            return "result"

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."


class TestIsFeatureAvailable:
    """Tests for is_feature_available helper."""

    def test_feature_not_available_community(self):
        """Should return False for Community edition."""
        reset_license_provider()

        assert is_feature_available(ProFeature.CI_CD_EXECUTION) is False
        assert is_feature_available(ProFeature.OUTPUT_JUNIT) is False

    def test_feature_available_pro(self):
        """Should return True for Pro edition."""
        reset_license_provider()

        def pro_provider():
            return License(edition=Edition.PRO)

        try:
            register_pro_license_provider(pro_provider)

            assert is_feature_available(ProFeature.CI_CD_EXECUTION) is True
            assert is_feature_available(ProFeature.OUTPUT_JUNIT) is True
        finally:
            reset_license_provider()


class TestProFeature:
    """Tests for the ProFeature enum."""

    def test_all_features_have_names(self):
        """All Pro features should have human-readable names."""
        from validibot.core.license import PRO_FEATURE_NAMES

        for feature in ProFeature:
            assert feature in PRO_FEATURE_NAMES, f"Feature {feature} missing name"
            assert PRO_FEATURE_NAMES[feature], f"Feature {feature} has empty name"

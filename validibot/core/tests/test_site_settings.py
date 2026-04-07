"""
Tests for platform-wide SiteSettings.

Verifies the singleton loading pattern, default field values, and the
metadata policy enforcement logic that protects the submission API from
oversized or overly-nested metadata payloads.
"""

import pytest

from validibot.core.models import MetadataPolicyError
from validibot.core.models import SiteSettings
from validibot.core.site_settings import get_site_settings

pytestmark = pytest.mark.django_db

DEFAULT_MAX_BYTES = 4096


class TestGetSiteSettings:
    """Verify the singleton loading helper."""

    def test_creates_default_record(self):
        """First call should create the singleton row with field defaults."""
        SiteSettings.objects.all().delete()
        obj = get_site_settings()
        assert obj.metadata_max_bytes == DEFAULT_MAX_BYTES
        assert obj.metadata_key_value_only is False
        assert SiteSettings.objects.count() == 1

    def test_returns_existing_record(self):
        """Subsequent calls should return the same row, not create a new one."""
        SiteSettings.objects.all().delete()
        first = get_site_settings()
        second = get_site_settings()
        assert first.pk == second.pk
        assert SiteSettings.objects.count() == 1


class TestEnforceMetadataPolicy:
    """Verify the metadata policy enforcement on the model."""

    def test_scalar_only_blocks_nested_dict(self):
        """When key-value-only is enabled, a nested dict should be rejected."""
        obj = SiteSettings(metadata_key_value_only=True)
        with pytest.raises(MetadataPolicyError):
            obj.enforce_metadata_policy({"nested": {"oops": True}})

    def test_scalar_only_blocks_nested_list(self):
        """When key-value-only is enabled, a list value should be rejected."""
        obj = SiteSettings(metadata_key_value_only=True)
        with pytest.raises(MetadataPolicyError):
            obj.enforce_metadata_policy({"tags": ["a", "b"]})

    def test_scalar_only_allows_scalars(self):
        """Scalar values (str, int, bool) should pass when enforcement is on."""
        obj = SiteSettings(metadata_key_value_only=True)
        obj.enforce_metadata_policy({"name": "test", "count": 5, "ok": True})

    def test_max_bytes_enforced(self):
        """Metadata exceeding the byte limit should be rejected."""
        obj = SiteSettings(metadata_max_bytes=10)
        with pytest.raises(MetadataPolicyError):
            obj.enforce_metadata_policy({"big": "x" * 50})

    def test_max_bytes_zero_disables_limit(self):
        """A zero byte limit should disable the size check entirely."""
        obj = SiteSettings(metadata_max_bytes=0)
        obj.enforce_metadata_policy({"big": "x" * 10000})

    def test_default_settings_allow_reasonable_metadata(self):
        """Default settings (4096 bytes, no key-value restriction) should
        allow normal metadata payloads."""
        obj = SiteSettings()
        obj.enforce_metadata_policy({"source": "api", "version": "1.0"})

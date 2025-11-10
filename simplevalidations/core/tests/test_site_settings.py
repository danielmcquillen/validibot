import pytest

from simplevalidations.core.models import SiteSettings
from simplevalidations.core.site_settings import APISubmissionSettings
from simplevalidations.core.site_settings import MetadataPolicyError
from simplevalidations.core.site_settings import get_site_settings


pytestmark = pytest.mark.django_db


def test_get_site_settings_creates_default_record():
    SiteSettings.objects.all().delete()
    settings = get_site_settings()
    assert settings.api_submission.metadata_max_bytes == 4096
    assert SiteSettings.objects.count() == 1


def test_get_site_settings_normalizes_invalid_data():
    SiteSettings.objects.update_or_create(
        slug=SiteSettings.DEFAULT_SLUG,
        defaults={
            "data": {
                "api_submission": {
                    "metadata_max_bytes": -5,
                },
            },
        },
    )
    settings = get_site_settings()
    assert settings.api_submission.metadata_max_bytes == 4096
    stored = SiteSettings.objects.get(slug=SiteSettings.DEFAULT_SLUG)
    assert stored.data["api_submission"]["metadata_max_bytes"] == 4096


def test_submission_settings_enforce_metadata_policy_scalar_only():
    config = APISubmissionSettings(metadata_key_value_only=True)
    with pytest.raises(MetadataPolicyError):
        config.enforce_metadata_policy({"nested": {"oops": True}})


def test_submission_settings_enforce_metadata_policy_max_bytes():
    config = APISubmissionSettings(metadata_max_bytes=10)
    with pytest.raises(MetadataPolicyError):
        config.enforce_metadata_policy({"big": "x" * 50})

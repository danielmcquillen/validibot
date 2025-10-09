from __future__ import annotations

import pytest
from django.template import TemplateSyntaxError
from django.test import override_settings

from simplevalidations.marketing.templatetags import marketing_flags


@pytest.mark.parametrize(
    ("feature_key", "setting_name", "expected"),
    (
        ("resources", "RESOURCES_ENABLED", True),
        ("docs", "DOCS_ENABLED", False),
        ("pricing", "PRICING_ENABLED", True),
        ("features", "FEATURES_ENABLED", False),
    ),
)
def test_marketing_feature_enabled(feature_key: str, setting_name: str, expected: bool) -> None:
    overrides = {
        "RESOURCES_ENABLED": True,
        "DOCS_ENABLED": False,
        "PRICING_ENABLED": True,
        "FEATURES_ENABLED": False,
    }
    with override_settings(**overrides):
        result = marketing_flags.marketing_feature_enabled(feature_key)
        assert result is expected


def test_marketing_feature_enabled_rejects_unknown_key() -> None:
    with pytest.raises(TemplateSyntaxError):
        marketing_flags.marketing_feature_enabled("unknown")

from types import SimpleNamespace

from django.test import RequestFactory

from simplevalidations.core.templatetags import core_tags
from simplevalidations.validations.constants import Severity


def _build_context(path="/app/validations/library/", view_name="validations:validation_library"):
    request = RequestFactory().get(path)
    if view_name and ":" in view_name:
        namespace, url_name = view_name.split(":", 1)
    else:
        namespace, url_name = "", view_name
    request.resolver_match = SimpleNamespace(
        namespace=namespace,
        view_name=view_name,
        url_name=url_name,
    )
    return {"request": request}


def test_active_link_views_returns_active_for_matching_view_name():
    context = _build_context()

    result = core_tags.active_link_views(
        context,
        "validations:validation_library",
        "validations:validator_detail",
    )

    assert result == "active"


def test_active_link_views_returns_empty_string_for_non_matching_view():
    context = _build_context(view_name="validations:validation_list")

    result = core_tags.active_link_views(
        context,
        "validations:validation_library",
    )

    assert result == ""


def test_active_link_views_handles_plain_url_names():
    context = _build_context(view_name="validations:validator_detail")

    result = core_tags.active_link_views(
        context,
        "validator_detail",
    )

    assert result == "active"


def test_finding_badge_class_returns_expected_mappings():
    error = SimpleNamespace(severity=Severity.ERROR)
    warning = SimpleNamespace(severity=Severity.WARNING)
    info = SimpleNamespace(severity=Severity.INFO)
    unknown = SimpleNamespace(severity="OTHER")

    assert core_tags.finding_badge_class(error) == "text-bg-danger"
    assert core_tags.finding_badge_class(warning) == "text-bg-warning text-dark"
    assert core_tags.finding_badge_class(info) == "text-bg-secondary"
    assert core_tags.finding_badge_class(unknown) == "text-bg-secondary"

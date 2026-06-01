from types import SimpleNamespace

from django.test import RequestFactory

from validibot.core.templatetags import core_tags
from validibot.validations.constants import Severity


def _build_context(
    path="/app/validations/library/",
    view_name="validations:validation_library",
):
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


def test_finding_failed_rows_formats_truncated_meta():
    """The tag turns a finding's meta into the "rows … (showing first N of M)"
    line the findings table renders.

    Why it matters: this is the user-visible payoff of capturing failing rows —
    the tag must read ``sample_rows`` + ``count`` and surface the truncation, or
    the report would silently hide that there are more failures than listed.
    """
    finding = SimpleNamespace(meta={"sample_rows": [1, 2, 4], "count": 12})

    assert core_tags.finding_failed_rows(finding) == (
        "rows 1, 2, 4 (showing first 3 of 12)"
    )


def test_finding_failed_rows_is_empty_for_findings_without_rows():
    """Findings that carry no row examples render nothing.

    JSON/XML/SHACL findings have no ``sample_rows``; the tag is used in a generic
    findings table, so it must return "" for them rather than error or print a
    stray label. A missing ``meta`` attribute entirely must also be safe.
    """
    assert core_tags.finding_failed_rows(SimpleNamespace(meta=None)) == ""
    assert core_tags.finding_failed_rows(SimpleNamespace(meta={})) == ""
    assert core_tags.finding_failed_rows(SimpleNamespace()) == ""


def test_finding_failed_rows_is_usable_from_a_template():
    """The tag is registered and callable via ``{% load core_tags %}``.

    Proves the template-side wiring (registration + ``as`` assignment), not just
    the Python function — that is what the findings partial actually relies on.
    """
    from django.template import Context
    from django.template import Template

    # One failing row at line 2: sample holds it and count agrees, so no marker.
    finding = SimpleNamespace(meta={"sample_rows": [2], "count": 1})
    template = Template(
        "{% load core_tags %}{% finding_failed_rows finding as r %}{{ r }}",
    )

    rendered = template.render(Context({"finding": finding}))

    assert rendered == "rows 2"

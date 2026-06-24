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
    """The tag turns a finding's meta into the "row numbers: … (showing first N
    of M)" line the findings table renders.

    Why it matters: this is the user-visible payoff of capturing failing rows —
    the tag must read ``sample_rows`` + ``count`` and surface the truncation, or
    the report would silently hide that there are more failures than listed.
    """
    finding = SimpleNamespace(meta={"sample_rows": [1, 2, 4], "count": 12})

    assert core_tags.finding_failed_rows(finding) == (
        "row numbers: 1, 2, 4 (showing first 3 of 12)"
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

    assert rendered == "row numbers: 2"


# head_init_script
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# The tag inlines the compiled, TypeScript-authored head-init snippet into
# <head> inside a nonce'd <script>. These tests pin three behaviours: it wraps
# the real compiled source with the CSP nonce, it degrades to empty output when
# the build artifact is missing (rather than erroring), and it caches the file
# read so it isn't hit on every request.


def _reset_head_init_cache():
    core_tags._head_init_cache.clear()


def test_head_init_script_inlines_compiled_source_with_nonce():
    """The tag wraps the compiled snippet in a nonce'd <script>.

    This is the whole point of the FOUC fix: the snippet must run synchronously
    in <head>, and to satisfy CSP it must carry the per-request nonce. We assert
    the nonce attribute is present and that a known token from the compiled
    source (the storage key) made it into the output verbatim — proving we
    inlined the real artifact, not an escaped or empty placeholder.
    """
    _reset_head_init_cache()
    try:
        rendered = core_tags.head_init_script({"CSP_NONCE": "test-nonce-123"})
    finally:
        _reset_head_init_cache()

    assert 'nonce="test-nonce-123"' in rendered
    assert "<script" in rendered
    assert "</script>" in rendered
    # The storage key lives in the compiled JS; its presence proves the real
    # build artifact was inlined (requires `npm run build:js` to have run).
    assert "validibot:leftNavCollapsed" in rendered


def test_head_init_script_is_empty_when_source_missing(monkeypatch):
    """A missing build artifact degrades to empty output, not an error.

    If `npm run build:js` hasn't run, the page must still render — just without
    before-paint nav priming. We simulate the missing file and assert the tag
    returns an empty string rather than raising.
    """
    _reset_head_init_cache()
    monkeypatch.setattr(core_tags, "_read_head_init_source", lambda: "")
    try:
        rendered = core_tags.head_init_script({"CSP_NONCE": "n"})
    finally:
        _reset_head_init_cache()

    assert rendered == ""


def test_head_init_source_is_cached_after_first_read(monkeypatch):
    """The file is read once and cached; later calls don't touch disk.

    The snippet is a build artifact that never changes between deploys, so
    re-reading it per request is wasted I/O. We count finder calls across two
    invocations and assert the second is served from cache.
    """
    _reset_head_init_cache()
    calls = {"n": 0}
    real_find = core_tags.finders.find

    def counting_find(path):
        calls["n"] += 1
        return real_find(path)

    monkeypatch.setattr(core_tags.finders, "find", counting_find)
    try:
        first = core_tags._read_head_init_source()
        second = core_tags._read_head_init_source()
    finally:
        _reset_head_init_cache()

    assert first == second
    # finders.find is called at most once (zero if served from STATIC_ROOT,
    # but never twice — the second call must hit the in-process cache).
    assert calls["n"] <= 1

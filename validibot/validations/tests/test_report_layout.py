"""Tests for the report-layout preference resolver.

``validations.services.report_layout.resolve_report_layout`` is the single
place that decides which run-report layout (stacked vs classic) a request sees,
and it is called by *two* views — the standalone run-detail page and the
launch-page status card — so both stay in sync. Its contract is small but
load-bearing:

- **Default is stacked.** A brand-new session with no ``?layout`` must land on
  the stacked layout; the whole point of this change is that both pages default
  to stacked.
- **A valid ``?layout`` both returns and *persists*.** The toggle works by
  appending ``?layout=`` and reloading; the choice has to stick to the session
  so the next page (without the param) honours it.
- **Garbage never escapes.** An unknown param or a corrupted stored value must
  fall back to the default rather than reach a template that would fail to find
  a matching layout partial.

Pure request handling, no DB — so ``SimpleTestCase`` with ``RequestFactory``.
"""

from __future__ import annotations

from django.test import RequestFactory
from django.test import SimpleTestCase

from validibot.validations.services.report_layout import DEFAULT_REPORT_LAYOUT
from validibot.validations.services.report_layout import resolve_report_layout


class ResolveReportLayoutTests(SimpleTestCase):
    """The single resolver both report-rendering views depend on."""

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, query: str = "", session: dict | None = None):
        """Build a GET request with a dict-backed session.

        A plain dict stands in for the session store: the resolver only does
        ``__getitem__`` / ``get`` / ``__setitem__``, so this keeps the test
        DB- and middleware-free while exercising the real persistence path.
        """
        request = self.factory.get(f"/?{query}" if query else "/")
        request.session = dict(session or {})
        return request

    def test_defaults_to_stacked(self):
        """No param and an empty session must yield the stacked default.

        This is the headline requirement — both pages default to stacked — so
        it is pinned directly against the exported default constant too.
        """
        assert resolve_report_layout(self._request()) == "stacked"
        assert DEFAULT_REPORT_LAYOUT == "stacked"

    def test_valid_param_is_returned_and_persisted(self):
        """``?layout=classic`` switches the layout AND remembers it.

        The toggle relies on persistence: it reloads with the param once, and
        every subsequent page (which has no param) must keep showing classic.
        """
        request = self._request("layout=classic")
        assert resolve_report_layout(request) == "classic"
        assert request.session["report_layout"] == "classic"

    def test_stored_preference_wins_without_param(self):
        """A stored choice is honoured when no param is present.

        Proves the "remember in the session" half of the contract — the user
        picked classic earlier, so a later param-less request stays classic.
        """
        request = self._request(session={"report_layout": "classic"})
        assert resolve_report_layout(request) == "classic"

    def test_param_overrides_stored_preference(self):
        """A fresh param beats the stored value and replaces it.

        Toggling back to stacked must both render stacked now and overwrite the
        stored classic, otherwise the toggle would feel one-way.
        """
        request = self._request(
            "layout=stacked",
            session={"report_layout": "classic"},
        )
        assert resolve_report_layout(request) == "stacked"
        assert request.session["report_layout"] == "stacked"

    def test_invalid_param_is_ignored(self):
        """An unknown ``?layout`` value is ignored, leaving the stored choice.

        Defends against a hand-typed or stale URL: ``?layout=bogus`` must not
        overwrite the user's real preference nor be returned verbatim.
        """
        request = self._request(
            "layout=bogus",
            session={"report_layout": "classic"},
        )
        assert resolve_report_layout(request) == "classic"
        assert request.session["report_layout"] == "classic"

    def test_corrupt_stored_value_falls_back_to_default(self):
        """A garbage stored value resolves to the default, never escapes.

        The return value is fed straight into ``{% if report_layout == ... %}``;
        if a corrupted session value leaked through, the template would match no
        layout partial and render nothing. Falling back to stacked keeps the
        report always renderable.
        """
        request = self._request(session={"report_layout": "garbage"})
        assert resolve_report_layout(request) == "stacked"

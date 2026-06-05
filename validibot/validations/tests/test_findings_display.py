"""Tests for the finding failing-row display helpers.

``validations.services.findings_display`` is the single place that turns a
finding's ``meta`` (the ``sample_rows`` + ``count`` produced by validators that
aggregate a bulk failure into one finding) into something user-facing. Both the
web template tag and the API serializer call it, so its behaviour is the
contract the UI and the API share — which is why it gets its own focused suite.

The two behaviours that matter:

- It is a **no-op for findings without row examples** (JSON/XML/SHACL and any
  finding whose ``meta`` lacks ``sample_rows``), so wiring it into a generic
  findings table can't break non-tabular findings.
- It reports **truncation honestly**: ``count`` is the authoritative total, the
  sample is only the first slice, and the "showing first N of M" marker appears
  exactly when the total exceeds the examples kept.

Pure functions, no DB — so ``SimpleTestCase``.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from validibot.validations.services.findings_display import abbreviate_iri
from validibot.validations.services.findings_display import finding_subject
from validibot.validations.services.findings_display import format_failed_rows
from validibot.validations.services.findings_display import group_step_findings
from validibot.validations.services.findings_display import summarize_failed_rows


class AbbreviateIriTests(SimpleTestCase):
    """Path-column display: shorten SHACL IRIs, leave JSON/XML paths alone.

    A SHACL finding's ``path`` is the ``sh:resultPath`` IRI, whose namespace
    repeats on every row and is noise. This shortens IRIs to their local name
    for display (with the full IRI kept on hover by the template), while leaving
    JSON pointers / XPaths untouched — collapsing those would destroy the very
    location information they exist to convey.
    """

    def test_iri_fragment_shortens_to_local_name(self):
        """An IRI with a ``#`` fragment shows just the local name.

        The 223P case: ``…standard223#hasConnectionPoint`` → ``hasConnectionPoint``,
        which is what made the Path column noisy across the whole report.
        """
        assert (
            abbreviate_iri("http://data.ashrae.org/standard223#hasConnectionPoint")
            == "hasConnectionPoint"
        )

    def test_slash_terminated_iri_shortens_to_last_segment(self):
        """A path-style IRI shortens on its final segment.

        Some vocabularies use ``/`` rather than ``#``; both must collapse so the
        column stays compact regardless of namespace convention.
        """
        assert abbreviate_iri("https://example.org/schema/Sensor") == "Sensor"

    def test_json_pointer_is_left_unchanged(self):
        """A JSON pointer is returned verbatim — its full path is the value.

        For the JSON-Schema validator the path *is* the location of the bad
        value; abbreviating ``/items/0/name`` to ``name`` would hide which item
        failed. The guard (no scheme, no ``#``) keeps it intact.
        """
        assert abbreviate_iri("/items/0/name") == "/items/0/name"

    def test_empty_and_none_return_empty_string(self):
        """Missing paths render as empty, never ``None``.

        The template prints this directly; returning ``""`` keeps a path-less
        finding from showing a literal ``None``.
        """
        assert abbreviate_iri("") == ""
        assert abbreviate_iri(None) == ""


class _FakeFinding:
    """A stand-in for ``ValidationFinding`` for the pure-function grouping tests.

    ``group_step_findings`` only reads ``code`` / ``message`` / ``path`` /
    ``meta`` and calls ``get_severity_display()``. Using a tiny fake keeps the
    suite DB-free (``SimpleTestCase``) while exercising the exact attribute
    surface the real model exposes — the grouping is presentation logic, not an
    ORM concern, so testing it against the surface rather than the DB row keeps
    the tests fast and focused.
    """

    def __init__(self, severity, message, code="", path="", meta=None):
        self._severity = severity
        self.message = message
        self.code = code
        self.path = path
        self.meta = meta or {}

    def get_severity_display(self) -> str:
        return self._severity


class SummarizeFailedRowsTests(SimpleTestCase):
    """The structured summary the API hands clients."""

    def test_none_meta_returns_none(self):
        """A finding with no meta has no row examples to summarise.

        This is the JSON/XML/SHACL path — those validators never set
        ``sample_rows`` — so the helper must return ``None`` rather than invent
        an empty summary that a caller might render as "0 rows".
        """
        self.assertIsNone(summarize_failed_rows(None))

    def test_meta_without_sample_rows_returns_none(self):
        """Meta that carries other keys but no ``sample_rows`` still yields None.

        A validator may put unrelated data in ``meta``; only the presence of
        ``sample_rows`` means "this finding represents specific rows".
        """
        self.assertIsNone(summarize_failed_rows({"column": "lat"}))

    def test_full_sample_is_not_truncated(self):
        """When the sample holds every failing row, ``truncated`` is False.

        ``count`` equals the sample length, so there is nothing hidden and the
        UI should not claim there is more.
        """
        summary = summarize_failed_rows({"sample_rows": [1, 2, 3], "count": 3})
        assert summary == {"sample_rows": [1, 2, 3], "count": 3, "truncated": False}

    def test_capped_sample_is_truncated(self):
        """A total larger than the sample marks the summary truncated.

        This is the headline case: the engine kept the first N example rows but
        ``count`` knows the real total, so the summary must signal that the list
        is partial.
        """
        summary = summarize_failed_rows({"sample_rows": [1, 2], "count": 12})
        assert summary == {"sample_rows": [1, 2], "count": 12, "truncated": True}

    def test_missing_count_falls_back_to_sample_length(self):
        """When a producer omits ``count`` we assume the sample is complete.

        Better to under-claim (no truncation marker) than to fabricate a total
        we can't substantiate — so the fallback is the sample length, not a
        guess.
        """
        summary = summarize_failed_rows({"sample_rows": [4, 5]})
        assert summary == {"sample_rows": [4, 5], "count": 2, "truncated": False}

    def test_count_below_sample_length_is_clamped(self):
        """A nonsensical ``count`` smaller than the sample never under-reports.

        Defensive: if a bug ever produced ``count`` < ``len(sample_rows)`` we
        clamp up to the sample length so we don't render "showing first 3 of 2".
        """
        summary = summarize_failed_rows({"sample_rows": [1, 2, 3], "count": 2})
        assert summary == {"sample_rows": [1, 2, 3], "count": 3, "truncated": False}


class FormatFailedRowsTests(SimpleTestCase):
    """The human string the template tag renders."""

    def test_no_rows_formats_to_empty_string(self):
        """No row examples -> empty string, so the template can hide the line.

        The findings table renders this for every finding; returning "" keeps
        non-tabular findings visually unchanged.
        """
        assert format_failed_rows(None) == ""
        assert format_failed_rows({"column": "lat"}) == ""

    def test_full_sample_lists_rows_without_a_marker(self):
        """A complete sample reads as a plain row list, no truncation noise.

        The ``row numbers:`` label disambiguates from a count —
        ``"row numbers: 1, 2, 4"`` can't be misread as
        "three rows" the way ``"rows 1, 2, 4"`` might.
        """
        assert format_failed_rows({"sample_rows": [1, 2, 4], "count": 3}) == (
            "row numbers: 1, 2, 4"
        )

    def test_truncated_sample_shows_first_n_of_total(self):
        """A capped sample makes the hidden remainder explicit.

        The reader sees both the examples and the true scale ("of 3412"), which
        is more useful than a bare "(truncated)" because it quantifies how much
        more there is.
        """
        assert format_failed_rows({"sample_rows": [1, 2, 3], "count": 3412}) == (
            "row numbers: 1, 2, 3 (showing first 3 of 3412)"
        )


class FindingSubjectTests(SimpleTestCase):
    """P0: the "which instance" a finding is about (the SHACL focus node).

    A finding's ``path`` says which *property* failed; the subject says which
    *node*. RDF has no line numbers, so the focus node IRI is the address — and
    surfacing it is the single biggest readability win for a SHACL report. These
    tests pin that it is extracted when present and is a strict no-op otherwise,
    which is what lets the same generic findings table stay unchanged for
    validators that don't carry a subject.
    """

    def test_none_meta_returns_none(self):
        """No meta -> no subject. This is the JSON/XML/tabular path.

        Guarantees adding a Subject column can't synthesise a phantom subject
        for validators that never set one — they keep rendering as before.
        """
        self.assertIsNone(finding_subject(None))

    def test_meta_without_subject_key_returns_none(self):
        """Meta with unrelated keys but no focus node still yields None.

        Only the dedicated subject key counts; arbitrary ``meta`` payload (e.g.
        a tabular finding's ``sample_rows``) must not be mistaken for a subject.
        """
        self.assertIsNone(finding_subject({"sample_rows": [1, 2]}))

    def test_focus_node_is_extracted_and_shortened(self):
        """A SHACL focus node yields the full IRI plus a compact last segment.

        The short form drops the repeated namespace noise that clutters every
        row, while the full IRI is preserved for a tooltip/copy so nothing the
        user might need to act on is lost.
        """
        subject = finding_subject(
            {"shacl_focus_node": "http://onuma.com/bldg-3593#GenericC_3210411"},
        )
        assert subject == {
            "subject": "http://onuma.com/bldg-3593#GenericC_3210411",
            "subject_short": "GenericC_3210411",
            "value": None,
        }

    def test_offending_value_is_surfaced_when_present(self):
        """When the constraint reported an offending term, it rides along.

        Output-stage SHACL results often carry the bad value (``shacl_value``);
        showing it next to the subject answers "what was wrong with it" without
        a second lookup.
        """
        subject = finding_subject(
            {
                "shacl_focus_node": "ex:conn1",
                "shacl_value": "ex:NotAConnectionPoint",
            },
        )
        assert subject["value"] == "ex:NotAConnectionPoint"

    def test_slash_terminated_iri_shortens_on_last_segment(self):
        """IRIs that use ``/`` rather than ``#`` shorten on the final segment.

        s223 uses ``#`` but Onuma/Brick IRIs use ``/``; the shortener must
        handle both so the Subject column stays compact regardless of namespace
        convention.
        """
        subject = finding_subject({"shacl_focus_node": "http://x.example/a/b/Node42"})
        assert subject["subject_short"] == "Node42"


class GroupStepFindingsTests(SimpleTestCase):
    """P1: collapse repeated rule violations; leave distinct findings alone.

    The contract that makes this safe for *every* validator (not just SHACL):
    a rule collapses only when ``(code, message, path)`` matches across
    findings, and a group of one renders exactly like today. So a SHACL report
    where the focus node varies collapses dramatically, while a JSON/XML report
    whose findings sit at different paths is untouched. These tests pin both
    halves of that contract, plus the severity sectioning the report relies on.
    """

    def test_repeated_shacl_rule_collapses_with_subjects(self):
        """The same SHACL rule across many nodes becomes one counted group.

        This is the headline case — a 223P building fires the same constraint
        for dozens of components. We assert the group carries the true count and
        one subject per member, so the UI can show "× N" and expand to the list.
        """
        findings = [
            _FakeFinding(
                "Error",
                "An ElectricityOutlet shall have exactly one inlet.",
                code="shacl.MinCountConstraintComponent",
                path="s223:hasConnectionPoint",
                meta={"shacl_focus_node": "ex:EO_1"},
            ),
            _FakeFinding(
                "Error",
                "An ElectricityOutlet shall have exactly one inlet.",
                code="shacl.MinCountConstraintComponent",
                path="s223:hasConnectionPoint",
                meta={"shacl_focus_node": "ex:EO_2"},
            ),
        ]
        grouped = group_step_findings(findings)

        assert grouped["show_subject"] is True
        assert len(grouped["severities"]) == 1
        groups = grouped["severities"][0]["groups"]
        assert len(groups) == 1
        assert groups[0]["count"] == 2  # noqa: PLR2004
        assert [s["subject_short"] for s in groups[0]["subjects"]] == ["EO_1", "EO_2"]

    def test_distinct_paths_do_not_collapse(self):
        """Same message at different paths stays as separate single rows.

        This is the JSON-Schema/XML safety property: because ``path`` is part of
        the rule identity, findings at different locations are *not* merged — so
        a validator whose findings each have a unique pointer sees no grouping.
        """
        findings = [
            _FakeFinding("Error", "Required.", code="json.required", path="/a/name"),
            _FakeFinding("Error", "Required.", code="json.required", path="/b/name"),
        ]
        grouped = group_step_findings(findings)

        assert grouped["show_subject"] is False
        groups = grouped["severities"][0]["groups"]
        assert len(groups) == 2  # noqa: PLR2004
        assert all(group["count"] == 1 for group in groups)

    def test_singletons_render_unchanged(self):
        """A finding that appears once is a group of one — no count, no subject.

        Proves the "only collapses real duplicates" promise: a non-SHACL report
        of all-unique findings produces one group per finding with ``count == 1``
        and ``show_subject`` False, i.e. the exact shape the old template drew.
        """
        findings = [
            _FakeFinding("Error", "Bad A.", code="x", path="/a"),
            _FakeFinding("Warning", "Bad B.", code="y", path="/b"),
        ]
        grouped = group_step_findings(findings)

        assert grouped["show_subject"] is False
        labels = [section["label"] for section in grouped["severities"]]
        assert labels == ["Error", "Warning"]
        for section in grouped["severities"]:
            assert len(section["groups"]) == 1
            assert section["groups"][0]["count"] == 1

    def test_exact_duplicates_without_subject_still_collapse(self):
        """Identical findings with no subject collapse to a single counted row.

        Even without a focus node, two byte-identical findings (same code +
        message + path) are noise when listed twice. Collapsing them keeps the
        report honest — and incidentally absorbs the double-surfacing of the
        same envelope finding if it ever recurs — while the count stays exact.
        """
        dupe = {
            "severity": "Error",
            "message": "A Connection shall be associated with a ConnectionPoint.",
            "code": "shacl.cnx",
            "path": "s223:cnx",
        }
        findings = [_FakeFinding(**dupe), _FakeFinding(**dupe)]
        grouped = group_step_findings(findings)

        groups = grouped["severities"][0]["groups"]
        assert len(groups) == 1
        assert groups[0]["count"] == 2  # noqa: PLR2004
        # No subject keys -> nothing to expand, and the Subject column stays off.
        assert grouped["show_subject"] is False
        assert groups[0]["subjects"] == []

    def test_severity_sections_preserve_input_order(self):
        """Severity sections appear in the order severities first occur.

        The findings queryset is already ordered ERROR -> WARNING -> INFO, and
        the report shows section headers in that order; grouping must not
        reshuffle them, or the headline errors would stop sitting at the top.
        """
        findings = [
            _FakeFinding(
                "Error", "e", code="c", path="p", meta={"shacl_focus_node": "ex:n1"}
            ),
            _FakeFinding("Warning", "w", code="c2", path="p2"),
            _FakeFinding(
                "Error", "e", code="c", path="p", meta={"shacl_focus_node": "ex:n2"}
            ),
        ]
        grouped = group_step_findings(findings)

        labels = [section["label"] for section in grouped["severities"]]
        assert labels == ["Error", "Warning"]
        # The two ERROR findings (same rule, different nodes) land in one group.
        error_groups = grouped["severities"][0]["groups"]
        assert len(error_groups) == 1
        assert error_groups[0]["count"] == 2  # noqa: PLR2004

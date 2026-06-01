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

from validibot.validations.services.findings_display import format_failed_rows
from validibot.validations.services.findings_display import summarize_failed_rows


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

        The ``row #s:`` label disambiguates from a count — ``"row #s: 1, 2, 4"``
        can't be misread as "three rows" the way ``"rows 1, 2, 4"`` might.
        """
        assert format_failed_rows({"sample_rows": [1, 2, 4], "count": 3}) == (
            "row #s: 1, 2, 4"
        )

    def test_truncated_sample_shows_first_n_of_total(self):
        """A capped sample makes the hidden remainder explicit.

        The reader sees both the examples and the true scale ("of 3412"), which
        is more useful than a bare "(truncated)" because it quantifies how much
        more there is.
        """
        assert format_failed_rows({"sample_rows": [1, 2, 3], "count": 3412}) == (
            "row #s: 1, 2, 3 (showing first 3 of 3412)"
        )

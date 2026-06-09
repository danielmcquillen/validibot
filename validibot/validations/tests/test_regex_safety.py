"""Tests for ``validibot.validations.regex_safety`` — RE2-backed regex execution.

### What this suite covers and why

Author-supplied regular expressions run against *submitter*-supplied data in
several validators. Python's :mod:`re` backtracks, so a pattern like ``(a+)+$``
against a crafted value is a denial-of-service (ReDoS) vector, and a thread
timeout cannot reliably stop it (CPython holds the GIL through one match). The
``regex_safety`` helper routes every author pattern through Google RE2, which
matches in linear time with no backtracking.

These tests pin the two properties the security of every caller depends on:

- **A catastrophic pattern cannot hang.** The classic backtracking bomb matches
  instantly — the test simply *completing* is the proof of linearity (under
  :mod:`re` this input would run effectively forever).
- **Unsupported patterns fail loudly, never silently.** RE2 omits backreferences
  and lookaround; such a pattern is rejected with a clear error rather than
  silently falling back to the unsafe :mod:`re` engine, which would reopen the
  hole and disagree about semantics.

Ordinary matching, the case-insensitive option, and compilation caching are
pinned too so the safe engine remains a drop-in for the callers it replaced.
"""

from __future__ import annotations

import pytest
from django.test import SimpleTestCase

from validibot.validations.regex_safety import UnsafeOrInvalidPatternError
from validibot.validations.regex_safety import compile_user_pattern


class RegexSafetyTests(SimpleTestCase):
    """RE2-backed compilation is linear-time, strict, and a drop-in matcher."""

    def test_catastrophic_pattern_matches_in_linear_time(self):
        """The textbook ReDoS bomb resolves instantly instead of hanging.

        ``(a+)+$`` against a long run of ``a`` followed by a non-matching
        character is the canonical exponential-backtracking case: under
        :mod:`re` it would run effectively forever and pin a CPU. RE2 has no
        backtracking, so it answers in linear time — this test *completing* is
        the regression guard (if a caller ever reverts to :mod:`re`, the suite
        hangs here, loudly signalling the security property broke).
        """
        compiled = compile_user_pattern(r"(a+)+$")
        # 100 'a's then '!' — 2**100 backtracking paths under a PCRE engine.
        self.assertIsNone(compiled.fullmatch("a" * 100 + "!"))
        self.assertIsNotNone(compiled.fullmatch("a" * 100))

    def test_backreference_pattern_is_rejected(self):
        """Backreferences (RE2 omits them) are rejected, not silently downgraded.

        Falling back to :mod:`re` for a backreference pattern would reintroduce
        the backtracking engine — exactly the ReDoS vector we are closing — so
        the helper must refuse it instead.
        """
        with pytest.raises(UnsafeOrInvalidPatternError):
            compile_user_pattern(r"(a)\1")

    def test_lookahead_pattern_is_rejected(self):
        """Lookahead is an RE2-unsupported Perl feature; reject it explicitly."""
        with pytest.raises(UnsafeOrInvalidPatternError):
            compile_user_pattern(r"foo(?=bar)")

    def test_lookbehind_pattern_is_rejected(self):
        """Lookbehind is likewise unsupported; reject rather than fall back."""
        with pytest.raises(UnsafeOrInvalidPatternError):
            compile_user_pattern(r"(?<=foo)bar")

    def test_syntactically_invalid_pattern_is_rejected(self):
        """A malformed expression raises the same author-facing error type."""
        with pytest.raises(UnsafeOrInvalidPatternError):
            compile_user_pattern(r"[unterminated")

    def test_error_message_is_author_facing(self):
        """The rejection explains *why* so an author can fix the pattern.

        The message names the safety trade-off (backreferences/lookaround are
        unavailable) rather than leaking a raw C-library error.
        """
        with pytest.raises(UnsafeOrInvalidPatternError) as exc_info:
            compile_user_pattern(r"(?<=foo)bar")
        assert "lookaround" in str(exc_info.value).lower()

    def test_ordinary_pattern_search_and_fullmatch(self):
        """A normal pattern matches with the same search/fullmatch semantics.

        ``search`` is unanchored (used by the Basic validator's ``matches``
        operator); ``fullmatch`` requires the whole string (Table Schema's
        ``pattern`` semantics). Both must behave like a compiled :mod:`re`.
        """
        compiled = compile_user_pattern(r"[A-Z]-\d")
        self.assertIsNotNone(compiled.fullmatch("A-1"))
        self.assertIsNone(compiled.fullmatch("A-1-x"))  # fullmatch is anchored
        self.assertIsNotNone(compiled.search("xx A-1 xx"))  # search is not

    def test_ignore_case_option(self):
        """``ignore_case`` mirrors ``re.IGNORECASE`` for the Basic validator."""
        self.assertIsNotNone(
            compile_user_pattern("abc", ignore_case=True).fullmatch("ABC"),
        )
        self.assertIsNone(
            compile_user_pattern("abc", ignore_case=False).fullmatch("ABC"),
        )

    def test_compilation_is_cached(self):
        """Equal calls return the cached compiled object (matching is the hot path).

        Callers such as the Basic validator compile per evaluation; caching keeps
        repeated evaluation of one assertion from recompiling. Case-folding is
        part of the cache key, so the two variants stay distinct.
        """
        self.assertIs(
            compile_user_pattern("shared-pattern"),
            compile_user_pattern("shared-pattern"),
        )
        self.assertIsNot(
            compile_user_pattern("shared-pattern"),
            compile_user_pattern("shared-pattern", ignore_case=True),
        )

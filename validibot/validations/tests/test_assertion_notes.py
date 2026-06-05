"""Model-layer guarantees for the ``RulesetAssertion.notes`` field.

Notes are author-entered free text, so they are an untrusted input surface. These
tests pin the two protections that live on the model — and therefore apply to
*every* write path that runs ``full_clean()`` (the assertion form via the mutation
service, and VAF import alike), not just the interactive form:

1. content sanitization (control-char/NUL stripping, newline normalization) that
   never corrupts the comparison syntax notes are made of, and
2. the ``max_length`` cap that bounds the storage/DoS surface.

Output-side XSS safety (autoescaping of notes on the assertion card) is covered
separately in ``workflows/tests/test_workflow_assertions.py``; here we prove the
*input* never reaches the database in a hostile or malformed shape.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from validibot.validations.constants import RULESET_ASSERTION_NOTES_MAX_LENGTH
from validibot.validations.tests.factories import RulesetAssertionFactory

pytestmark = pytest.mark.django_db


# ── Sanitization runs inside full_clean() ───────────────────────────────────
# clean() is where the sanitizer lives, so any path that validates before saving
# gets it for free. We mutate a clean factory instance and re-validate to isolate
# the notes behaviour from the rest of the assertion contract.
def test_full_clean_strips_control_characters_from_notes():
    """A NUL/control byte in notes is removed during validation, not saved raw.

    A NUL would otherwise abort the Postgres ``text`` insert; stripping it in
    clean() means an author (or a crafted import) can't wedge a row that fails
    to save or renders as mojibake.
    """
    assertion = RulesetAssertionFactory()
    assertion.notes = "before\x00\x07after"
    assertion.full_clean()
    assert assertion.notes == "beforeafter"


def test_full_clean_normalizes_newlines_and_trims_notes():
    """CRLF endings fold to ``\\n`` and surrounding whitespace is trimmed."""
    assertion = RulesetAssertionFactory()
    assertion.notes = "  line1\r\nline2  "
    assertion.full_clean()
    assert assertion.notes == "line1\nline2"


def test_full_clean_preserves_comparison_and_markup_syntax():
    """Notes describing comparisons keep their ``<``/``>`` — no tag stripping.

    This is the regression guard against "sanitizing" notes with strip_tags:
    the field exists to document rules like "value <= 100" and "List<int>", so
    that content must survive validation verbatim (output escaping makes it safe
    to display).
    """
    rationale = "Fails when value <max> exceeds the List<int> bound (x < 5)."
    assertion = RulesetAssertionFactory()
    assertion.notes = rationale
    assertion.full_clean()
    assert assertion.notes == rationale


# ── Length cap ──────────────────────────────────────────────────────────────
def test_full_clean_rejects_notes_over_max_length():
    """Notes longer than 5000 chars fail validation on every write path.

    The cap is a validation-layer rule (Postgres ``text`` ignores max_length),
    so the protection only holds because each write path calls full_clean();
    this asserts the validator is actually wired to the field.
    """
    assertion = RulesetAssertionFactory()
    assertion.notes = "x" * (RULESET_ASSERTION_NOTES_MAX_LENGTH + 1)
    with pytest.raises(ValidationError) as exc_info:
        assertion.full_clean()
    assert "notes" in exc_info.value.message_dict


def test_full_clean_allows_notes_at_max_length():
    """A note exactly at the limit validates (the boundary is inclusive)."""
    assertion = RulesetAssertionFactory()
    assertion.notes = "x" * RULESET_ASSERTION_NOTES_MAX_LENGTH
    assertion.full_clean()  # must not raise
    assert len(assertion.notes) == RULESET_ASSERTION_NOTES_MAX_LENGTH

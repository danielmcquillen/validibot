"""Tests for the ratcheting production-code mypy baseline.

The baseline is a CI trust boundary: historical diagnostics may remain while
new type errors must fail. These tests pin normalization and comparison so a
formatting change cannot silently turn the guard into a no-op.
"""

from collections import Counter

from scripts.check_mypy_baseline import baseline_regressions
from scripts.check_mypy_baseline import parse_mypy_errors


def test_parse_mypy_errors_ignores_lines_and_groups_codes() -> None:
    """Line movement must not create debt while file/error categories remain."""
    output = """validibot/example.py:10: error: Bad assignment  [assignment]
validibot/example.py:99:5: error: Another assignment  [assignment]
validibot/other.py:2: error: Untyped problem
validibot/example.py:10: note: This is context only
Found 3 errors in 2 files (checked 10 source files)"""

    assert parse_mypy_errors(output) == Counter(
        {
            "validibot/example.py|assignment": 2,
            "validibot/other.py|uncoded": 1,
        }
    )


def test_baseline_regressions_allow_reductions_but_reject_growth() -> None:
    """Fixes pass automatically; a new category or larger count fails."""
    allowed = Counter({"a.py|assignment": 3, "b.py|union-attr": 1})
    current = Counter(
        {
            "a.py|assignment": 2,
            "b.py|union-attr": 2,
            "c.py|attr-defined": 1,
        }
    )

    assert baseline_regressions(current=current, allowed=allowed) == {
        "b.py|union-attr": (1, 2),
        "c.py|attr-defined": (0, 1),
    }

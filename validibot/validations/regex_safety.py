"""Safe execution of author-supplied regular expressions.

Workflow authors configure regex patterns that run against *submitter*-supplied
data — the Basic validator's ``regex`` operator and the Tabular validator's
per-column ``pattern`` constraint, today. Python's standard :mod:`re` uses a
backtracking engine, so a pattern such as ``(a+)+$`` against a crafted value
backtracks exponentially and can pin a worker for a very long time — a ReDoS
(regular-expression denial-of-service) vector. A thread-based timeout does *not*
reliably stop it: CPython holds the GIL through a single ``re`` match, so the
runaway thread keeps a CPU core busy until the match finishes (measured: a 0.5 s
``ThreadPoolExecutor`` timeout returned only after the full 1.59 s match).

This module routes every author-supplied pattern through Google RE2
(:mod:`re2`), whose automaton matches in time linear in the input with **no
backtracking** — so a pathological pattern is impossible by construction rather
than merely time-boxed.

The trade-off is dialect: RE2 deliberately omits the Perl features that make
backtracking necessary — backreferences (``\\1``) and lookaround (``(?=)`` /
``(?<=)``). A pattern using them is rejected with :class:`UnsafeOrInvalidPatternError`
at author time, surfaced to the author, and **never** silently retried with
:mod:`re` — that fallback is exactly the engine the ReDoS risk comes from, and
the two engines disagree about semantics besides.
"""

from __future__ import annotations

import functools

import re2
from django.utils.translation import gettext as _

# Cap the compiled-pattern cache. Distinct author patterns are few in practice;
# the bound just keeps an adversarial flood of unique patterns from growing the
# cache without limit.
_COMPILE_CACHE_SIZE = 512


class UnsafeOrInvalidPatternError(ValueError):
    """An author-supplied regex RE2 will not compile.

    Raised for a syntactically invalid expression *and* for one that uses a
    feature RE2 omits by design (backreferences, lookahead, lookbehind). Callers
    surface the message to the author; they must not fall back to :mod:`re`.
    """


def _decode_detail(exc: re2.error) -> str:
    """Return RE2's error detail as text (it arrives as ``bytes``)."""
    detail = exc.args[0] if exc.args else exc
    if isinstance(detail, bytes):
        return detail.decode("utf-8", "replace")
    return str(detail)


@functools.lru_cache(maxsize=_COMPILE_CACHE_SIZE)
def compile_user_pattern(pattern: str, *, ignore_case: bool = False):
    """Compile *pattern* with RE2 (linear-time, backtracking-free).

    Args:
        pattern: The author-supplied regular expression.
        ignore_case: Match case-insensitively (RE2 ``Options.case_sensitive``),
            mirroring :data:`re.IGNORECASE` for the Basic validator.

    Returns:
        A compiled RE2 pattern exposing ``search``/``fullmatch`` like a compiled
        :mod:`re` object, but matching in time linear in the input.

    Raises:
        UnsafeOrInvalidPatternError: The pattern is invalid or uses a Perl
            feature RE2 omits (backreferences, lookaround).

    Compilation is cached because compiling is the cost — matching is cheap — so
    re-evaluating the same assertion across many rows does not recompile. Invalid
    patterns are not cached (the exception re-raises), which is fine: they are
    rare and rejected at author time.
    """
    options = re2.Options()
    # Surface parse failures as a Python exception, not as RE2's C-level absl
    # logging to stderr (which would spam worker logs on an author typo).
    options.log_errors = False
    if ignore_case:
        options.case_sensitive = False
    try:
        return re2.compile(pattern, options)
    except re2.error as exc:
        msg = _(
            "Pattern is not a supported regular expression: %(detail)s. For "
            "safety, patterns are matched with RE2, which does not support "
            "backreferences or lookaround.",
        ) % {"detail": _decode_detail(exc)}
        raise UnsafeOrInvalidPatternError(msg) from exc

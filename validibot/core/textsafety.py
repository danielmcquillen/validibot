"""Sanitization for free-form user prose that is stored verbatim, escaped on output.

Validibot stores user-authored text (assertion notes and similar) as-is and relies
on Django's template autoescaping and JSON encoding to neutralize any markup at
render time. That is the OWASP-recommended posture — *store raw, encode on output*
— because the rendering context (HTML, JSON, an email) decides what needs escaping,
and the same stored string is safe in all of them once each output layer encodes it.

These helpers therefore do only the input cleaning that is **always** safe and can
never corrupt legitimate content:

- strip NUL and other C0 control characters (and DEL), which have no meaning in a
  text field and can crash a Postgres ``text`` insert (NUL) or break a terminal /
  log line, and
- normalize newlines and trim surrounding whitespace.

Deliberately NOT done here: HTML tag stripping. ``django.utils.html.strip_tags``
would turn ``"expects a List<int> here"`` into ``"expects a List here"`` and
``"value <max> threshold"`` into ``"value  threshold"`` — silently eating the
comparison and generic syntax that fields like assertion notes are full of. Output
escaping already makes such content safe to *display*, so stripping it on the way
*in* would only destroy data. Use this module for plain-text fields; reach for
``strip_tags`` only where HTML is genuinely never a legitimate input (e.g. a
support-contact message).
"""

from __future__ import annotations

import re

from django.utils.text import normalize_newlines

# C0 control characters (0x00–0x1F) plus DEL (0x7F), EXCEPT the whitespace we keep:
# tab (0x09) and line feed (0x0A). Carriage return (0x0D) is folded to 0x0A by
# normalize_newlines() before this runs, so it never reaches the strip.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_plain_text(value: str | None) -> str:
    """Return *value* cleaned for safe storage as a plain-text field.

    Removes control characters / NUL (which can fail a Postgres ``text`` insert or
    corrupt rendering), normalizes ``\\r\\n`` and ``\\r`` to ``\\n``, and trims
    surrounding whitespace. Printable characters — including ``<``, ``>`` and other
    markup-looking content — are preserved untouched; the responsibility for making
    that content safe belongs to the output layer (template autoescaping, JSON).

    ``None`` and empty input both return ``""`` so callers can assign the result
    straight onto a ``blank=True, default=""`` field.
    """
    if not value:
        return ""
    return _CONTROL_CHARS.sub("", normalize_newlines(value)).strip()

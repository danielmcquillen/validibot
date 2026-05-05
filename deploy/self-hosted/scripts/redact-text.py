"""Standalone redactor used by ``just gcp collect-support-bundle``.

The GCP support-bundle recipe runs on the operator's workstation
(``gcloud`` is local, the validibot venv typically isn't), so it
can't shell into the deployed image's ``python manage.py
redact_text``. This script is a stdlib-only copy of the redaction
patterns that ``validibot.core.support_bundle.redact_text_for_bundle``
applies.

Reads stdin, writes redacted text to stdout. Identical UNIX-pipe
shape to the management command — same calling sites work either
way.

Pattern-drift contract
======================

The patterns below are a verbatim copy of those in
``validibot/core/support_bundle.py``'s ``_LOG_REDACTION_PATTERNS``.
A kit-shape test asserts the two stay in sync. If you edit one,
edit the other and re-run ``pytest tests/test_self_hosted_kit.py
-k log_redaction``.

We deliberately don't import ``validibot.core.support_bundle`` here
because that requires Django + Pydantic + the project venv.
Operators on a fresh workstation just have gcloud and Python —
this script must work in that environment without setup.
"""

from __future__ import annotations

import re
import sys

REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(authorization|x-api-key|x-auth-token)\s*[:=][^\r\n]*",
        ),
        r"\1: [REDACTED]",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.-]+"), "Bearer [REDACTED]"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        "[REDACTED-JWT]",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED-PEM-PRIVATE-KEY]",
    ),
    (re.compile(r"://([^/:@\s]+):([^@\s]+)@"), r"://[REDACTED]:[REDACTED]@"),
    (
        re.compile(r"(?<!sha256:)\b[A-Fa-f0-9]{32,}\b"),
        "[REDACTED-HEX]",
    ),
    (
        re.compile(
            r"(?i)\b((?:[A-Z_]+_)?(?:SECRET|PASSWORD|PASSWD|TOKEN|API[_-]?KEY|"
            r"PRIVATE[_-]?KEY|CREDENTIAL|WEBHOOK[_-]?SECRET|SIGNING[_-]?KEY)"
            r"[A-Z_]*)\s*[:=]\s*([^\s,;}\[]+)",
        ),
        r"\1=[REDACTED]",
    ),
)


def redact(text: str) -> str:
    for pattern, replacement in REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


if __name__ == "__main__":
    sys.stdout.write(redact(sys.stdin.read()))

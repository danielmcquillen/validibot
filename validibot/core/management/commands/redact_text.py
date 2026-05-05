"""Filter free-form text through the support-bundle redaction patterns.

Used by ``just self-hosted collect-support-bundle`` and
``just gcp collect-support-bundle <stage>`` to scrub container logs
and gcloud output before zipping them into a support bundle. Reads
from stdin, writes to stdout — UNIX-pipe shape so the recipes can
chain it after ``docker compose logs`` or ``gcloud logging read``
without intermediate files.

The redaction logic itself lives in
:func:`validibot.core.support_bundle.redact_text_for_bundle` so it can
be unit-tested separately from this thin CLI wrapper.

Usage::

    docker compose logs --tail=200 web | python manage.py redact_text > recent-web.log
    gcloud logging read ... | python manage.py redact_text > recent-cloud-logs.txt

Why a Django management command rather than a standalone script:
operators run this inside the web container (where ``manage.py`` is
the ergonomic entry point) and the redaction module imports through
the same Pydantic/Django stack the rest of support-bundle uses. A
standalone script would duplicate the import setup.
"""

from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from validibot.core.support_bundle import redact_text_for_bundle


class Command(BaseCommand):
    """Read text from stdin, write redacted text to stdout.

    Catches the common operator-data-leakage classes the support-
    bundle recipe would otherwise zip into the bundle verbatim:
    bearer tokens, JWTs, PEM private keys, URLs with embedded basic
    auth, hex-shaped secrets, and ``KEY=value`` / ``PASSWORD: value``
    patterns where the key name is itself sensitive.

    Idempotent — re-running on already-redacted text produces the
    same output. The redaction sentinels (``[REDACTED]``,
    ``[REDACTED-JWT]``, etc.) are designed to NOT match any pattern.
    """

    help = (
        "Apply support-bundle redaction patterns to text on stdin. "
        "Outputs redacted text to stdout."
    )

    def handle(self, *args, **options):
        # ``sys.stdin.read()`` reads everything at once. Log files are
        # typically a few hundred KB; the multi-GB case isn't a real
        # concern because the calling recipes pass ``--tail=N`` to
        # ``docker compose logs`` / freshness limits to ``gcloud
        # logging read``. If a future caller needs streaming
        # redaction, switching to ``for line in sys.stdin`` is a
        # one-line change — but every pattern would have to commit
        # to single-line matches, which the multi-line PEM pattern
        # specifically does not.
        raw = sys.stdin.read()
        redacted = redact_text_for_bundle(raw)
        # Use ``self.stdout.write`` to play nicely with Django's
        # output handling, then fall through without an extra
        # newline (``write`` doesn't add one; the redacted text
        # already ends with whatever the input ended with).
        self.stdout.write(redacted, ending="")

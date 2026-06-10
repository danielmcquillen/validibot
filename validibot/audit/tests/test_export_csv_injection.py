"""Regression tests for CSV / formula-injection in the audit-log export.

The audit log stores attacker-influenced strings verbatim — an
``actor_email`` captured from a failed-login attempt, or a
``target_repr`` snapshotting a user-named object. When those rows are
exported as CSV and opened in a spreadsheet (Excel, LibreOffice, Google
Sheets), any cell whose first character is ``= + - @`` (or a leading
tab / carriage return) is evaluated as a *formula* rather than shown as
text. That is CSV / formula injection (CWE-1236): a value like
``=cmd|'/c calc'!A1`` can execute on open.

These tests pin the fix in :mod:`validibot.audit.views`, which prefixes
any such cell with a single quote so every major spreadsheet treats it
as literal text. They matter because the regression is invisible in the
happy path — a benign export looks identical — so without an explicit
test a future refactor of the row serialiser could silently drop the
guard and reintroduce a remote-ish code-execution vector for whoever
opens the export.

We keep this in a uniquely named file (not ``test_export.py``) so the
security regression cannot be accidentally deleted alongside an
unrelated format-correctness change.
"""

from __future__ import annotations

import csv
import io
from http import HTTPStatus

from django.core.cache import cache
from django.test import Client
from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry
from validibot.audit.views import _csv_safe
from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.tests.factories import MembershipFactory
from validibot.users.tests.utils import ensure_all_roles_exist

# A non-string sentinel used to prove ``_csv_safe`` leaves non-text cells
# (e.g. integer ``actor_user_id`` / ``target_id`` columns) untouched. Named
# rather than inlined to satisfy the no-magic-number rule (PLR2004).
_NON_STRING_CELL = 42


def _login_with_membership(client: Client, membership) -> None:
    """Log the membership's user in with ``active_org`` resolved.

    Mirrors the helper in ``test_export.py`` so the export view sees a
    real org scope; without an active org the view 404s before it ever
    streams a row.
    """

    user = membership.user
    user.set_current_org(membership.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = membership.org.id
    session.save()


def _pro_license() -> License:
    """A Pro licence that advertises the AUDIT_LOG feature.

    The export endpoint is gated behind ``CommercialFeature.AUDIT_LOG``;
    without this the request 404s at the feature gate and we never reach
    the CSV serialiser under test.
    """

    return License(
        edition=Edition.PRO,
        features=frozenset({CommercialFeature.AUDIT_LOG.value}),
    )


class CsvSafeHelperTests(SimpleTestCase):
    """Unit-level coverage of the ``_csv_safe`` neutraliser.

    Faster and more exhaustive than the HTTP test for the character
    matrix, so we assert the full trigger set here and reserve the view
    test for proving the guard is actually wired into the export path.
    """

    def test_formula_triggers_are_prefixed_and_benign_values_untouched(self) -> None:
        """Every spreadsheet formula trigger must be quoted; nothing else.

        This is the core security property: a leading ``= + - @``, tab,
        or carriage return is forced to literal text via a ``'`` prefix,
        while ordinary values (and non-string cells such as integer ids
        or ``None``) pass through byte-for-byte so the export stays
        faithful for non-spreadsheet consumers.
        """

        for payload in ("=cmd()", "+1+1", "-2+3", "@SUM(A1)", "\tnudge", "\rnudge"):
            with self.subTest(payload=payload):
                assert _csv_safe(payload) == f"'{payload}"

        # Benign strings and non-strings are returned unchanged — the
        # guard must never mutate legitimate audit data.
        assert _csv_safe("actor@example.com is fine mid-string") == (
            "actor@example.com is fine mid-string"
        )
        assert _csv_safe("Some Workflow") == "Some Workflow"
        assert _csv_safe(_NON_STRING_CELL) == _NON_STRING_CELL
        assert _csv_safe(None) is None


class AuditExportCsvInjectionViewTests(TestCase):
    """End-to-end proof the export view neutralises a hostile actor field."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def setUp(self) -> None:
        set_license(_pro_license())
        cache.clear()  # reset the per-org export rate-limit counter
        self.membership = MembershipFactory()
        _login_with_membership(self.client, self.membership)

    def test_malicious_actor_email_is_neutralised_in_csv_export(self) -> None:
        """An ``actor_email`` of ``=cmd()`` must be written as ``'=cmd()``.

        This is the real-world attack: an unauthenticated actor seeds a
        failed-login record whose username/email is a formula, then an
        admin later exports the audit log. The streamed CSV must contain
        the single-quote-prefixed, inert form — proving the guard is
        applied to the actual response body, not just available as a
        helper.
        """

        actor = AuditActor.objects.create(email="=cmd()")
        AuditLogEntry.objects.create(
            actor=actor,
            org=self.membership.org,
            action=AuditAction.LOGIN_FAILED.value,
            target_type="users.User",
            target_id="1",
            target_repr="victim",
        )

        response = self.client.get(reverse("audit:export") + "?format=csv")
        assert response.status_code == HTTPStatus.OK

        body = b"".join(response.streaming_content).decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(body)))
        assert len(rows) == 1
        # The neutralised cell keeps its original characters but gains the
        # leading apostrophe that disarms formula evaluation on open.
        assert rows[0]["actor_email"] == "'=cmd()"
        # And the raw, dangerous form must not survive as a bare formula.
        assert ",=cmd()" not in body

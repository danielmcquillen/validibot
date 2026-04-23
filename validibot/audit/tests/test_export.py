"""Extensive tests for ``AuditLogExportView`` and filter-on-list.

Three themes:

1. **Access control** — the export endpoint inherits the same
   gating stack as the list view (login, feature flag, org scope).
   Regressions here would be straight-up data-leak territory.
2. **Format correctness** — CSV headers, JSONL one-object-per-line,
   Content-Type + Content-Disposition, filename shape. Downstream
   tooling (pandas, ``jq``) relies on this.
3. **Rate limiting** — 10/hr/org as fixed by the ADR. The 11th
   request in a window must 429 with a ``Retry-After`` header.
4. **Filter integration** — list view respects the filter query
   string; export respects the same filter; the "Export current
   view" UX works.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

from django.core.cache import cache
from django.test import Client
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry
from validibot.audit.views import _EXPORT_RATE_LIMIT
from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.tests.factories import MembershipFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.utils import ensure_all_roles_exist


def _login_with_membership(client: Client, membership) -> None:
    """Log the membership's user in with active_org set."""

    user = membership.user
    user.set_current_org(membership.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = membership.org.id
    session.save()


def _pro_license() -> License:
    return License(
        edition=Edition.PRO,
        features=frozenset({CommercialFeature.AUDIT_LOG.value}),
    )


def _create_entry(
    *,
    org,
    action: AuditAction = AuditAction.WORKFLOW_UPDATED,
    actor_email: str = "actor@example.com",
    target_type: str = "workflows.Workflow",
    target_repr: str = "Some Workflow",
    changes: dict | None = None,
    occurred_offset: timedelta = timedelta(),
) -> AuditLogEntry:
    """Seed an audit entry. Offset controls ``occurred_at`` for date tests."""

    actor = AuditActor.objects.create(email=actor_email)
    entry = AuditLogEntry.objects.create(
        actor=actor,
        org=org,
        action=action.value,
        target_type=target_type,
        target_id="1",
        target_repr=target_repr,
        changes=changes,
    )
    if occurred_offset:
        AuditLogEntry.objects.filter(pk=entry.pk).update(
            occurred_at=timezone.now() + occurred_offset,
        )
        entry.refresh_from_db()
    return entry


# ──────────────────────────────────────────────────────────────────
# Filter integration on the LIST view
# ──────────────────────────────────────────────────────────────────


class FilterIntegrationOnListViewTests(TestCase):
    """Filter form populates querystring → list view narrows results."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def setUp(self) -> None:
        set_license(_pro_license())
        cache.clear()  # fresh counters between tests
        self.membership = MembershipFactory()
        _login_with_membership(self.client, self.membership)

    def test_filter_by_action_narrows_list(self) -> None:
        """Querying ``?action=login_succeeded`` must return only
        login entries.
        """

        _create_entry(
            org=self.membership.org,
            action=AuditAction.WORKFLOW_UPDATED,
            target_repr="Workflow Entry",
        )
        _create_entry(
            org=self.membership.org,
            action=AuditAction.LOGIN_SUCCEEDED,
            target_repr="Login Entry",
        )

        response = self.client.get(
            reverse("audit:list") + "?action=login_succeeded",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Login Entry")
        self.assertNotContains(response, "Workflow Entry")

    def test_filter_by_actor_narrows_list(self) -> None:
        """``?actor=alice`` should match on ``actor.email`` icontains."""

        _create_entry(
            org=self.membership.org,
            actor_email="alice@example.com",
            target_repr="Alice Did This",
        )
        _create_entry(
            org=self.membership.org,
            actor_email="bob@example.com",
            target_repr="Bob Did That",
        )

        response = self.client.get(reverse("audit:list") + "?actor=alice")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alice Did This")
        self.assertNotContains(response, "Bob Did That")

    def test_filter_by_target_type_uses_exact_match(self) -> None:
        """Exact-match on ``target_type`` prevents a
        ``workflows.Workflow`` search from pulling in
        ``workflows.WorkflowStep``.
        """

        _create_entry(
            org=self.membership.org,
            target_type="workflows.Workflow",
            target_repr="Main Target",
        )
        _create_entry(
            org=self.membership.org,
            target_type="workflows.WorkflowStep",
            target_repr="Step Target",
        )

        response = self.client.get(
            reverse("audit:list") + "?target_type=workflows.Workflow",
        )
        self.assertContains(response, "Main Target")
        self.assertNotContains(response, "Step Target")

    def test_combined_filters_compose(self) -> None:
        """Filters stack as AND — all of (action, actor, target_type)
        applied together should narrow the result set further than
        any one alone.
        """

        _create_entry(
            org=self.membership.org,
            action=AuditAction.WORKFLOW_UPDATED,
            actor_email="alice@example.com",
            target_type="workflows.Workflow",
            target_repr="Alice Workflow",
        )
        # Same action but different actor — excluded by actor filter.
        _create_entry(
            org=self.membership.org,
            action=AuditAction.WORKFLOW_UPDATED,
            actor_email="bob@example.com",
            target_repr="Bob Workflow",
        )
        # Same actor but different action — excluded by action filter.
        _create_entry(
            org=self.membership.org,
            action=AuditAction.LOGIN_SUCCEEDED,
            actor_email="alice@example.com",
            target_repr="Alice Login",
        )

        response = self.client.get(
            reverse("audit:list")
            + "?action=workflow_updated&actor=alice"
            + "&target_type=workflows.Workflow",
        )
        self.assertContains(response, "Alice Workflow")
        self.assertNotContains(response, "Bob Workflow")
        self.assertNotContains(response, "Alice Login")

    def test_invalid_filter_shows_form_error(self) -> None:
        """A malformed date range must render the list page (200) with
        the error, not 500 or silently zero rows.
        """

        _create_entry(org=self.membership.org, target_repr="Should Still Render")

        response = self.client.get(
            reverse("audit:list") + "?date_from=2026-12-31&date_to=2026-01-01",
        )
        self.assertEqual(response.status_code, 200)
        # The form error surfaces in the template's non_field_errors
        # block.
        self.assertContains(
            response,
            "Start date must not be after end date.",
        )

    def test_export_button_links_carry_current_filters(self) -> None:
        """Clicking "Export CSV" with an active filter should land on
        the export URL with the same query string. The template
        composes ``export_querystring`` from ``request.GET``.
        """

        _create_entry(org=self.membership.org)

        response = self.client.get(
            reverse("audit:list") + "?action=login_succeeded",
        )
        content = response.content.decode()
        # The export link uses ``format=csv`` plus the original
        # filter querystring (HTML-encoded ``&`` as ``&amp;``).
        self.assertIn(
            reverse("audit:export") + "?format=csv&amp;action=login_succeeded",
            content,
        )


# ──────────────────────────────────────────────────────────────────
# Export view — access control matrix
# ──────────────────────────────────────────────────────────────────


class ExportAccessTests(TestCase):
    """The export endpoint inherits the same gate stack as the list."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def setUp(self) -> None:
        set_license(_pro_license())
        cache.clear()

    def test_anonymous_redirects_to_login(self) -> None:
        response = self.client.get(reverse("audit:export"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_community_license_renders_404(self) -> None:
        """No AUDIT_LOG → 404 (not 403). URL doesn't exist as far as
        the caller is concerned.
        """

        set_license(License(edition=Edition.COMMUNITY))
        membership = MembershipFactory()
        _login_with_membership(self.client, membership)
        response = self.client.get(reverse("audit:export"))
        self.assertEqual(response.status_code, 404)

    def test_unknown_format_returns_400(self) -> None:
        """``?format=xml`` gets rejected early with a useful message."""

        membership = MembershipFactory()
        _login_with_membership(self.client, membership)
        response = self.client.get(reverse("audit:export") + "?format=xml")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Unsupported export format", response.content)

    def test_other_orgs_entries_not_included(self) -> None:
        """The org-scope invariant applies to exports too — an entry
        owned by another org must NOT appear in this member's export.
        This is the tenancy-leak regression guard.
        """

        membership = MembershipFactory()
        _login_with_membership(self.client, membership)
        other_org = OrganizationFactory()
        _create_entry(
            org=other_org,
            target_repr="Cross-Org Canary",
        )
        _create_entry(
            org=membership.org,
            target_repr="Own Org Entry",
        )

        response = self.client.get(reverse("audit:export") + "?format=jsonl")
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode()
        self.assertIn("Own Org Entry", body)
        self.assertNotIn("Cross-Org Canary", body)


# ──────────────────────────────────────────────────────────────────
# Export view — format correctness
# ──────────────────────────────────────────────────────────────────


class ExportFormatTests(TestCase):
    """CSV header, JSONL line shape, Content-Disposition, filename."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def setUp(self) -> None:
        set_license(_pro_license())
        cache.clear()
        self.membership = MembershipFactory()
        _login_with_membership(self.client, self.membership)

    def _stream_to_string(self, response) -> str:
        """Drain a StreamingHttpResponse into a string."""

        return b"".join(response.streaming_content).decode()

    def test_csv_response_has_right_content_headers(self) -> None:
        """CSV exports must be ``text/csv`` with an attachment filename
        — anything else and the browser will try to render instead of
        download.
        """

        response = self.client.get(reverse("audit:export") + "?format=csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("validibot-audit-", response["Content-Disposition"])
        self.assertIn(".csv", response["Content-Disposition"])

    def test_csv_body_parses_with_stdlib_csv(self) -> None:
        """A downstream reader (Excel, pandas, ``csv`` module) must be
        able to parse the body. Guards against accidental BOMs,
        unquoted commas in values, malformed header etc.
        """

        _create_entry(
            org=self.membership.org,
            action=AuditAction.WORKFLOW_UPDATED,
            target_repr="Entry, With Comma",
            changes={"name": {"before": "Old", "after": "New"}},
        )

        response = self.client.get(reverse("audit:export") + "?format=csv")
        body = self._stream_to_string(response)

        reader = csv.DictReader(io.StringIO(body))
        rows = list(reader)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["action"], AuditAction.WORKFLOW_UPDATED.value)
        self.assertEqual(row["target_repr"], "Entry, With Comma")
        # The changes column is JSON-encoded so the dict round-trips.
        self.assertEqual(
            json.loads(row["changes"]),
            {"name": {"before": "Old", "after": "New"}},
        )

    def test_jsonl_response_has_right_content_headers(self) -> None:
        """JSONL uses the newline-delimited JSON media type."""

        response = self.client.get(reverse("audit:export") + "?format=jsonl")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-ndjson")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn(".jsonl", response["Content-Disposition"])

    def test_jsonl_body_has_one_object_per_line(self) -> None:
        """Every non-empty line must be valid JSON. That's the whole
        JSONL contract — ``jq .`` streaming requires it.
        """

        _create_entry(org=self.membership.org, target_repr="A")
        _create_entry(org=self.membership.org, target_repr="B")
        _create_entry(org=self.membership.org, target_repr="C")

        response = self.client.get(reverse("audit:export") + "?format=jsonl")
        body = self._stream_to_string(response)

        lines = [line for line in body.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        for line in lines:
            parsed = json.loads(line)
            self.assertIn("action", parsed)
            self.assertIn("occurred_at", parsed)

    def test_export_respects_filter(self) -> None:
        """A filtered export should contain only matching rows — the
        key invariant for "Export current view" UX.
        """

        _create_entry(
            org=self.membership.org,
            action=AuditAction.WORKFLOW_UPDATED,
            target_repr="Workflow Entry",
        )
        _create_entry(
            org=self.membership.org,
            action=AuditAction.LOGIN_SUCCEEDED,
            target_repr="Login Entry",
        )

        response = self.client.get(
            reverse("audit:export") + "?format=jsonl&action=login_succeeded",
        )
        self.assertEqual(response.status_code, 200)
        body = self._stream_to_string(response)

        self.assertIn("Login Entry", body)
        self.assertNotIn("Workflow Entry", body)

    def test_erased_actor_rendered_as_placeholder(self) -> None:
        """When an actor has been erased via the Phase-3 privacy
        workflow, the export must render ``(erased)`` rather than
        silently leaking the residual ``user_id``.
        """

        actor = AuditActor.objects.create(
            email="original@example.com",
            erased_at=timezone.now(),
        )
        AuditLogEntry.objects.create(
            actor=actor,
            org=self.membership.org,
            action=AuditAction.LOGIN_SUCCEEDED.value,
        )

        response = self.client.get(reverse("audit:export") + "?format=jsonl")
        body = self._stream_to_string(response)

        parsed = json.loads(body.splitlines()[0])
        self.assertEqual(parsed["actor_email"], "(erased)")
        self.assertEqual(parsed["actor_ip_address"], "(erased)")
        # Original email MUST NOT appear anywhere.
        self.assertNotIn("original@example.com", body)


# ──────────────────────────────────────────────────────────────────
# Rate limiting
# ──────────────────────────────────────────────────────────────────


class ExportRateLimitTests(TestCase):
    """10 requests per hour per org. 11th must 429 with Retry-After."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def setUp(self) -> None:
        set_license(_pro_license())
        cache.clear()
        self.membership = MembershipFactory()
        _login_with_membership(self.client, self.membership)
        _create_entry(org=self.membership.org)

    def test_requests_under_limit_all_succeed(self) -> None:
        """Under the hourly cap, every request returns 200 — the rate
        check must not emit false positives.
        """

        for i in range(_EXPORT_RATE_LIMIT):
            response = self.client.get(reverse("audit:export") + "?format=jsonl")
            self.assertEqual(
                response.status_code,
                200,
                f"Request {i + 1} unexpectedly rate-limited.",
            )

    def test_request_over_limit_returns_429(self) -> None:
        """The first request past the cap 429s with a ``Retry-After``
        header so well-behaved clients back off automatically.
        """

        # Exhaust the budget first.
        for _ in range(_EXPORT_RATE_LIMIT):
            self.client.get(reverse("audit:export") + "?format=jsonl")

        response = self.client.get(reverse("audit:export") + "?format=jsonl")
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)
        self.assertEqual(response["Retry-After"], "3600")

    def test_limit_is_scoped_per_org(self) -> None:
        """A second org's user must not be throttled by the first org's
        budget — the cache key is org id, not global.
        """

        # Org A hits the limit.
        for _ in range(_EXPORT_RATE_LIMIT + 1):
            self.client.get(reverse("audit:export") + "?format=jsonl")

        # Now a different org's member on the same app should be
        # unaffected.
        other_membership = MembershipFactory()
        _create_entry(org=other_membership.org)

        other_client = Client()
        _login_with_membership(other_client, other_membership)
        response = other_client.get(reverse("audit:export") + "?format=jsonl")
        self.assertEqual(response.status_code, 200)


class ExportUrlReverseTests(TestCase):
    """URL reverse guard against future ``app_name`` drift."""

    def test_export_url_reverse(self) -> None:
        self.assertEqual(reverse("audit:export"), "/app/audit/export/")

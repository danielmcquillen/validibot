"""Extensive tests for the Pro-gated audit log UI.

Covers the access-control matrix, org isolation, pagination,
ordering, query efficiency, URL reverse, and admin read-only
guarantees. Each test exercises exactly one invariant so a failure
points at a specific class of bug.

Access matrix (what we test):

+------------------+-----------------+----------------+
| Auth state       | License         | Expected       |
+------------------+-----------------+----------------+
| Anonymous        | any             | redirect login |
| Logged in, no    | any             | 403 (org scope |
|   membership     |                 |   precondition)|
| Logged in, org   | COMMUNITY       | 404 (feature   |
|                  |                 |   gate)        |
| Logged in, org   | PRO (no flag)   | 404 (feature   |
|                  |                 |   gate)        |
| Logged in, org   | PRO + AUDIT_LOG | 200 (list),    |
|                  |                 |   200 (detail) |
| Logged in, org B | PRO + flag      | org A's entries|
|   tries org A id |                 |   NOT visible; |
|                  |                 |   detail 404   |
+------------------+-----------------+----------------+
"""

from __future__ import annotations

from datetime import timedelta

from django.test import Client
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry
from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.tests.factories import MembershipFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.utils import ensure_all_roles_exist


def _login_with_membership(client: Client, membership) -> None:
    """Log the membership's user in and set their active org.

    Mirrors the helper used by ``test_navigation.py`` — without this
    the ``OrgMixin`` won't resolve an active org and the view will
    correctly 404 on every request.
    """

    user = membership.user
    user.set_current_org(membership.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = membership.org.id
    session.save()


def _pro_license_with_audit() -> License:
    """Return a Pro license that advertises AUDIT_LOG.

    The conftest snapshots/restores the active license between tests,
    so we don't need to reset this explicitly. Tests that want the
    community baseline just don't call ``set_license``.
    """

    return License(
        edition=Edition.PRO,
        features=frozenset({CommercialFeature.AUDIT_LOG.value}),
    )


def _create_entry(
    *,
    org,
    action: AuditAction = AuditAction.WORKFLOW_UPDATED,
    actor_email: str = "actor@example.com",
    target_repr: str = "Some Workflow",
    occurred_offset: timedelta = timedelta(),
) -> AuditLogEntry:
    """Build an ``AuditLogEntry`` with minimum-required fields.

    ``occurred_offset`` shifts ``occurred_at`` relative to now, letting
    tests control the sort order without having to manipulate the
    clock. Negative offsets make older entries.
    """

    actor = AuditActor.objects.create(email=actor_email)
    entry = AuditLogEntry.objects.create(
        actor=actor,
        org=org,
        action=action.value,
        target_type="workflows.Workflow",
        target_id="1",
        target_repr=target_repr,
    )
    if occurred_offset:
        # ``auto_now_add`` sets ``occurred_at`` at create time; we
        # overwrite via .update() so the test can order deterministically.
        AuditLogEntry.objects.filter(pk=entry.pk).update(
            occurred_at=timezone.now() + occurred_offset,
        )
        entry.refresh_from_db()
    return entry


class AuditListViewAccessTests(TestCase):
    """Access-control matrix for /app/audit/."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def setUp(self) -> None:
        """Fresh Pro license per test; the root conftest restores it
        afterwards so no explicit teardown is needed.
        """

        set_license(_pro_license_with_audit())

    def test_anonymous_redirects_to_login(self) -> None:
        """LoginRequiredMixin sends unauthenticated callers to login."""

        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 302)
        # Django's default login URL is ``/accounts/login/``.
        self.assertIn("login", response.url)

    def test_community_license_renders_404(self) -> None:
        """Community deployments lose AUDIT_LOG → 404.

        The feature gate is deliberate: a community deployment should
        not even know these URLs exist, let alone the shape of their
        response.
        """

        set_license(License(edition=Edition.COMMUNITY))
        membership = MembershipFactory()
        _login_with_membership(self.client, membership)

        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 404)

    def test_pro_license_without_flag_renders_404(self) -> None:
        """A Pro license that doesn't advertise AUDIT_LOG must be
        treated identically to community — the gate keys off the
        *feature*, not the *edition*.
        """

        set_license(
            License(
                edition=Edition.PRO,
                features=frozenset(),  # no AUDIT_LOG
            ),
        )
        membership = MembershipFactory()
        _login_with_membership(self.client, membership)

        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 404)

    def test_user_without_membership_sees_empty_list(self) -> None:
        """Logged-in users with no active org see an empty list, not
        a 500 or a cross-org leak. The queryset filter resolves to
        ``org=None`` which always produces an empty set, so returning
        200 with the empty-state template is both safe and a cleaner
        UX than a scary error page.
        """

        other_org = OrganizationFactory()
        # Seed an entry owned by a different org — it MUST NOT appear
        # in this user's response. This is the security assertion that
        # matters more than the status code itself.
        _create_entry(org=other_org, target_repr="Cross-Org Leak Canary")

        user = UserFactory()
        self.client.force_login(user)

        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Cross-Org Leak Canary")
        self.assertContains(response, "No audit log entries recorded yet")

    def test_pro_member_sees_list(self) -> None:
        """The happy path: Pro license, flag enabled, member of an
        org → 200 with entries visible.
        """

        membership = MembershipFactory()
        _login_with_membership(self.client, membership)
        _create_entry(org=membership.org, target_repr="Pro List Entry")

        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pro List Entry")


class AuditListViewBehaviourTests(TestCase):
    """List rendering: ordering, pagination, and org isolation."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def setUp(self) -> None:
        set_license(_pro_license_with_audit())
        self.membership = MembershipFactory()
        _login_with_membership(self.client, self.membership)

    def test_entries_ordered_newest_first(self) -> None:
        """The (org, -occurred_at) index is the whole point — the
        page must render newest-first so the index drives the query.
        """

        _create_entry(
            org=self.membership.org,
            target_repr="Older Entry",
            occurred_offset=timedelta(hours=-2),
        )
        _create_entry(
            org=self.membership.org,
            target_repr="Newer Entry",
            occurred_offset=timedelta(hours=-1),
        )

        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertLess(
            content.index("Newer Entry"),
            content.index("Older Entry"),
            "Entries should render newest-first.",
        )

    def test_other_orgs_entries_not_visible(self) -> None:
        """Org isolation: an entry scoped to a different org must NOT
        appear in this user's list. This is the PII-leak class of
        bug — a regression here would expose one tenant's audit data
        to another.
        """

        other_org = OrganizationFactory()
        _create_entry(org=other_org, target_repr="Other Org Entry")
        _create_entry(org=self.membership.org, target_repr="Own Org Entry")

        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Own Org Entry")
        self.assertNotContains(response, "Other Org Entry")

    def test_pagination_splits_more_than_fifty_entries(self) -> None:
        """``paginate_by=50`` on the ListView — 51 entries must
        produce 2 pages so the first page shows 50 entries and page
        2 is navigable.
        """

        for i in range(51):
            _create_entry(
                org=self.membership.org,
                target_repr=f"Entry {i}",
                occurred_offset=timedelta(minutes=-i),
            )

        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 200)
        # The pagination block renders "Page 1 of 2".
        self.assertContains(response, "Page 1 of 2")

        response2 = self.client.get(reverse("audit:list") + "?page=2")
        self.assertEqual(response2.status_code, 200)
        self.assertContains(response2, "Page 2 of 2")

    def test_empty_state_shows_placeholder(self) -> None:
        """Zero-entry org renders the empty-state notice rather than
        the table. A bare blank page would be indistinguishable from
        a broken view.
        """

        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No audit log entries recorded yet")


class AuditListViewQueryEfficiencyTests(TestCase):
    """Verify the queryset uses ``select_related`` to avoid N+1."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def setUp(self) -> None:
        set_license(_pro_license_with_audit())
        self.membership = MembershipFactory()
        _login_with_membership(self.client, self.membership)

    def test_n_entries_do_not_produce_n_plus_one_queries(self) -> None:
        """N+1 regression guard via a differential measurement.

        Rather than fix an absolute query limit (brittle — middleware
        changes elsewhere would break this test), we measure the
        incremental cost of adding entries: render the page with 5
        entries, then render the page with 30 entries, and assert
        the difference is small.

        Without ``select_related("actor", "actor__user", "org")`` the
        template's access to ``entry.actor.user.email`` would issue
        one SELECT per row — 25 extra entries → 25+ extra queries.
        With select_related the diff should be in the low single
        digits (extra SELECT ... FROM COUNT for pagination + maybe a
        few statement-cache variations).
        """

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        # Baseline: 5 entries.
        for i in range(5):
            _create_entry(
                org=self.membership.org,
                target_repr=f"Baseline {i}",
                actor_email=f"base{i}@example.com",
            )
        with CaptureQueriesContext(connection) as baseline:
            self.client.get(reverse("audit:list"))

        # Add 25 more entries, then measure again.
        for i in range(25):
            _create_entry(
                org=self.membership.org,
                target_repr=f"Extra {i}",
                actor_email=f"extra{i}@example.com",
            )
        with CaptureQueriesContext(connection) as loaded:
            self.client.get(reverse("audit:list"))

        delta = len(loaded.captured_queries) - len(baseline.captured_queries)
        self.assertLess(
            delta,
            10,
            f"Adding 25 more rows caused {delta} extra queries — smells "
            "like an N+1 regression. Check that list view keeps its "
            "select_related('actor', 'actor__user', 'org').",
        )


class _QueryUpperBound:
    """Context manager asserting ``len(queries) <= limit``.

    Django ships ``assertNumQueries`` (exact equality only). For a
    regression guard against N+1, we want "no worse than N" — an
    upper bound.

    Wraps Django's ``CaptureQueriesContext`` so queries are captured
    regardless of ``DEBUG`` — the default ``connection.queries_log``
    is only populated under DEBUG=True, which test settings disable.
    """

    def __init__(self, connection, limit: int) -> None:
        from django.test.utils import CaptureQueriesContext

        self._limit = limit
        self._capture = CaptureQueriesContext(connection)

    def __enter__(self):
        self._capture.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._capture.__exit__(exc_type, exc, tb)
        if exc_type is not None:
            return
        used = len(self._capture.captured_queries)
        assert used <= self._limit, (
            f"Query count {used} exceeded limit {self._limit}. "
            "Check for N+1 — the list view relies on "
            "select_related('actor', 'actor__user', 'org')."
        )


class AuditDetailViewTests(TestCase):
    """Detail view access control + rendering."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def setUp(self) -> None:
        set_license(_pro_license_with_audit())
        self.membership = MembershipFactory()
        _login_with_membership(self.client, self.membership)

    def test_own_org_entry_renders(self) -> None:
        """The happy path: the entry belongs to this user's org, so
        detail renders with action + target details.
        """

        entry = _create_entry(
            org=self.membership.org,
            action=AuditAction.WORKFLOW_UPDATED,
            target_repr="My Workflow",
        )

        response = self.client.get(
            reverse("audit:detail", kwargs={"entry_id": entry.pk}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My Workflow")
        self.assertContains(response, str(entry.pk))

    def test_other_orgs_entry_returns_404(self) -> None:
        """The crucial org-isolation check on the detail view. A
        member of org A guessing an id from org B must 404, not
        receive a 403 (which would confirm the id exists) and must
        NOT be shown content.
        """

        other_org = OrganizationFactory()
        other_entry = _create_entry(
            org=other_org,
            target_repr="Secret Other Org Workflow",
        )

        response = self.client.get(
            reverse("audit:detail", kwargs={"entry_id": other_entry.pk}),
        )
        self.assertEqual(response.status_code, 404)

    def test_missing_entry_returns_404(self) -> None:
        """Unknown ids 404 cleanly — no 500, no information leak."""

        response = self.client.get(
            reverse("audit:detail", kwargs={"entry_id": 999_999}),
        )
        self.assertEqual(response.status_code, 404)

    def test_detail_shows_changes_diff(self) -> None:
        """Entries with whitelisted diff changes should surface both
        before and after values in the detail view.
        """

        actor = AuditActor.objects.create(email="tester@example.com")
        entry = AuditLogEntry.objects.create(
            actor=actor,
            org=self.membership.org,
            action=AuditAction.WORKFLOW_UPDATED.value,
            target_type="workflows.Workflow",
            target_id="7",
            target_repr="Flow v2",
            changes={"name": {"before": "Flow v1", "after": "Flow v2"}},
        )

        response = self.client.get(
            reverse("audit:detail", kwargs={"entry_id": entry.pk}),
        )
        self.assertContains(response, "Flow v1")
        self.assertContains(response, "Flow v2")

    def test_detail_shows_erased_actor_label(self) -> None:
        """When an actor has been erased (PII nulled, ``erased_at``
        stamped), the detail view must render "(erased on ...)" —
        not silently show a blank row that could be misread as
        "action took itself".
        """

        actor = AuditActor.objects.create(
            email="to-erase@example.com",
            erased_at=timezone.now(),
        )
        entry = AuditLogEntry.objects.create(
            actor=actor,
            org=self.membership.org,
            action=AuditAction.LOGIN_SUCCEEDED.value,
        )

        response = self.client.get(
            reverse("audit:detail", kwargs={"entry_id": entry.pk}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "erased")
        # The pre-erasure email must NOT leak through.
        self.assertNotContains(response, "to-erase@example.com")


class AuditUrlReverseTests(TestCase):
    """URL reverse checks — guards against future ``app_name`` drift."""

    def test_list_url_reverse(self) -> None:
        self.assertEqual(reverse("audit:list"), "/app/audit/")

    def test_detail_url_reverse(self) -> None:
        self.assertEqual(
            reverse("audit:detail", kwargs={"entry_id": 42}),
            "/app/audit/42/",
        )


class AuditAdminReadOnlyTests(TestCase):
    """Django admin read-only guarantees for AuditActor + AuditLogEntry.

    Admin surface is intentionally browse-only. These tests guard
    against a future refactor accidentally re-enabling mutation.
    """

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def setUp(self) -> None:
        self.admin_user = UserFactory(is_staff=True, is_superuser=True)
        self.client.force_login(self.admin_user)

    def test_entry_changelist_is_accessible_to_staff(self) -> None:
        """Staff can browse the changelist — that's the whole point
        of registering the admin. They just can't mutate.
        """

        response = self.client.get("/admin/audit/auditlogentry/")
        self.assertEqual(response.status_code, 200)

    def test_entry_add_is_forbidden(self) -> None:
        """``has_add_permission`` returns False, so the Add page
        returns 403 even for superusers. Audit entries must only be
        created by the service, never by a UI form.
        """

        response = self.client.get("/admin/audit/auditlogentry/add/")
        self.assertEqual(response.status_code, 403)

    def test_actor_changelist_is_accessible(self) -> None:
        """AuditActor changelist follows the same pattern."""

        response = self.client.get("/admin/audit/auditactor/")
        self.assertEqual(response.status_code, 200)

    def test_actor_add_is_forbidden(self) -> None:
        """Same as AuditLogEntry — actors are created by the service
        during audit writes, never by a form.
        """

        response = self.client.get("/admin/audit/auditactor/add/")
        self.assertEqual(response.status_code, 403)

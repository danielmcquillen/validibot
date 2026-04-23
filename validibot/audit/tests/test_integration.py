"""End-to-end integration tests for the Phase-1 audit pipeline.

Walks the full path through the signal receivers + service with real
models. Verifies that:

1. Creating / updating / deleting a ``Workflow`` produces the correct
   ``AuditAction`` entries with whitelisted diffs.
2. Updating only non-whitelisted fields produces NO entry (quiet audit
   log — the log captures signal-of-interest events, not every row touch).
3. The admin bridge mirrors ``admin.LogEntry`` rows into
   ``AuditLogEntry`` with ``ADMIN_OBJECT_CHANGED`` + the admin action
   flavour in ``metadata``.
4. A login → workflow edit sequence inside an HTTP request produces
   two audit entries that share the same ``request_id`` — that's the
   correlation key Pro users will rely on in Phase 2's UI to group
   related events.

These tests depend on ``AuditConfig.ready()`` having run (signal
receivers attached, builtin registrations in place). The Django test
runner calls ``ready()`` during setup, so the receivers are live.
"""

from __future__ import annotations

from django.contrib.admin.models import ADDITION
from django.contrib.admin.models import CHANGE
from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.http import HttpResponse
from django.test import RequestFactory
from django.test import TestCase

from validibot.audit.constants import AuditAction
from validibot.audit.middleware import AuditContextMiddleware
from validibot.audit.models import AuditLogEntry
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.workflows.tests.factories import WorkflowFactory


class WorkflowLifecycleAuditTests(TestCase):
    """Create / update / delete a Workflow and inspect the audit log."""

    def setUp(self) -> None:
        """Wipe the audit table so each test starts clean."""
        AuditLogEntry.objects.all().delete()

    def test_workflow_create_produces_created_entry(self) -> None:
        """Saving a brand-new Workflow fires post_save(created=True)
        → WORKFLOW_CREATED entry. No diff payload — creates don't
        have a before state.
        """

        workflow = WorkflowFactory(name="New Flow")

        entries = list(
            AuditLogEntry.objects.filter(
                action=AuditAction.WORKFLOW_CREATED.value,
                target_id=str(workflow.pk),
            ),
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].target_type, "workflows.Workflow")
        # Creates don't carry a diff — the full state is implicit.
        self.assertIsNone(entries[0].changes)

    def test_workflow_update_captures_whitelisted_diff(self) -> None:
        """Updating a whitelisted field produces an UPDATED entry with
        the before/after values. Non-whitelisted fields stay out of
        ``changes`` via the whitelist, not via redaction — the snapshot
        never captures them in the first place.
        """

        workflow = WorkflowFactory(name="Before Name", description="before desc")
        # Clear the CREATED entry from factory setup to keep the
        # post-update assertion unambiguous.
        AuditLogEntry.objects.all().delete()

        workflow.name = "After Name"
        workflow.description = "after desc"
        workflow.save()

        entry = AuditLogEntry.objects.get(
            action=AuditAction.WORKFLOW_UPDATED.value,
        )
        self.assertEqual(
            entry.changes["name"],
            {"before": "Before Name", "after": "After Name"},
        )
        self.assertEqual(
            entry.changes["description"],
            {"before": "before desc", "after": "after desc"},
        )

    def test_workflow_update_with_no_whitelisted_changes_is_silent(self) -> None:
        """Saving a Workflow where only non-whitelisted fields changed
        produces NO audit entry. Otherwise every touch of
        ``modified`` or timestamp-ish metadata would clutter the log.
        """

        workflow = WorkflowFactory(name="Stable Name")
        AuditLogEntry.objects.all().delete()

        # Touch a field that is NOT in the whitelist — ``description``
        # IS whitelisted, so we can't use it here. The TimeStampedModel
        # ``modified`` timestamp is never whitelisted.
        workflow.save(update_fields=["modified"])

        self.assertEqual(AuditLogEntry.objects.count(), 0)

    def test_workflow_deletion_captures_final_state(self) -> None:
        """``pre_delete`` runs before the row is gone, so the audit
        entry can carry the Workflow's whitelisted fields as their
        final ``before`` values.
        """

        workflow = WorkflowFactory(
            name="Doomed Flow",
            description="about to be deleted",
        )
        workflow_pk = workflow.pk
        AuditLogEntry.objects.all().delete()

        workflow.delete()

        entry = AuditLogEntry.objects.get(
            action=AuditAction.WORKFLOW_DELETED.value,
        )
        self.assertEqual(entry.target_id, str(workflow_pk))
        # The diff captures "what the row looked like at deletion" —
        # ``before`` is the pre-delete state, ``after`` is None.
        self.assertEqual(
            entry.changes["name"],
            {"before": "Doomed Flow", "after": None},
        )


class AdminBridgeTests(TestCase):
    """Admin LogEntry rows mirror into AuditLogEntry."""

    def setUp(self) -> None:
        AuditLogEntry.objects.all().delete()
        self.user = UserFactory(is_staff=True)
        self.org = OrganizationFactory()

    def test_admin_addition_mirrors_to_audit_log(self) -> None:
        """Creating a ``LogEntry`` with ``ADDITION`` flag produces an
        ``ADMIN_OBJECT_CHANGED`` audit entry with the admin action
        kind in metadata.
        """

        content_type = ContentType.objects.get_for_model(self.org.__class__)

        LogEntry.objects.create(
            user=self.user,
            content_type=content_type,
            object_id=str(self.org.pk),
            object_repr=str(self.org),
            action_flag=ADDITION,
            change_message="",
        )

        entry = AuditLogEntry.objects.get(
            action=AuditAction.ADMIN_OBJECT_CHANGED.value,
        )
        self.assertEqual(entry.actor.user, self.user)
        self.assertEqual(entry.metadata["admin_action"], "added")
        # Target snapshot preserved so Phase 2 can render a readable
        # row even after the underlying object is gone.
        self.assertEqual(entry.target_repr, str(self.org))

    def test_admin_change_includes_change_message(self) -> None:
        """Admin CHANGE records carry a human-readable change message
        — we preserve it in metadata so Phase 2 can display the same
        summary Django admin's history view would.
        """

        content_type = ContentType.objects.get_for_model(self.org.__class__)

        LogEntry.objects.create(
            user=self.user,
            content_type=content_type,
            object_id=str(self.org.pk),
            object_repr=str(self.org),
            action_flag=CHANGE,
            change_message=('[{"changed": {"fields": ["Name"]}}]'),
        )

        entry = AuditLogEntry.objects.get(
            action=AuditAction.ADMIN_OBJECT_CHANGED.value,
        )
        self.assertEqual(entry.metadata["admin_action"], "changed")
        # ``get_change_message()`` returns the humanised version of
        # the JSON change_message we stored above.
        self.assertIn("Name", entry.metadata["change_message"])


class RequestIdCorrelationTests(TestCase):
    """Entries written during one HTTP request share a ``request_id``."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        AuditLogEntry.objects.all().delete()

    def test_login_and_workflow_edit_share_request_id(self) -> None:
        """A view that triggers multiple audit events inside one
        request must produce entries that share the middleware's
        ``request_id``. That's what Pro UI (Phase 2) will group on
        to show "events in this session".
        """

        user = UserFactory()

        def view(request):
            """Fake login + workflow edit inside a single request."""
            # Use login signal rather than full auth flow so this
            # test stays focused on audit correlation.
            from django.contrib.auth.signals import user_logged_in

            user_logged_in.send(
                sender=user.__class__,
                request=request,
                user=user,
            )
            # Now create a workflow so the model_audit path fires.
            WorkflowFactory(name="From Request", description="req test")
            return HttpResponse()

        middleware = AuditContextMiddleware(view)
        request = self.factory.post("/accounts/login/")
        request.user = user

        middleware(request)

        # Both actions should have produced entries with a non-empty
        # request_id, and the values should match.
        login_entry = AuditLogEntry.objects.get(
            action=AuditAction.LOGIN_SUCCEEDED.value,
        )
        workflow_entry = AuditLogEntry.objects.get(
            action=AuditAction.WORKFLOW_CREATED.value,
        )

        self.assertTrue(login_entry.request_id.startswith("req_"))
        self.assertEqual(login_entry.request_id, workflow_entry.request_id)

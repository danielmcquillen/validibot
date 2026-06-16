"""Integration tests for the Wave-2 org / membership / guest / validator
audit capture points.

These events extend the generic model-audit registry and add two
PII-careful signal receivers. Each test clears the audit table *after*
arranging fixtures and *before* the action under test, because creating
the fixtures themselves now emits audit entries (e.g. ``UserFactory``
provisions a personal ``Organization`` → ``ORG_CREATED``). Clearing at
that point keeps every assertion about exactly the action being tested.

Coverage:

* ``MEMBER_REMOVED`` — ``Membership`` delete (registry ``delete=``). The
  two ``membership.delete()`` views fire ``pre_delete`` without any
  view-level code.
* ``ORG_UPDATED`` / ``ORG_DELETED`` — ``Organization`` lifecycle. Creation
  is intentionally *not* audited (most orgs are auto-provisioned personal
  workspaces, and auditing create would mean an org's audit view is never
  empty). The ``org_resolver=lambda o: o`` override files the entry under
  the org itself so it shows in that org's own audit view.
* ``VALIDATOR_ADDED`` — ``WorkflowStep`` create, with the owning org
  resolved through ``step.workflow.org``.
* ``MEMBER_INVITED`` — ``MemberInvite`` create. The invitee's email is
  third-party PII and must never reach the entry; ``roles`` and
  ``status`` are captured instead.
* ``GUEST_GRANTED`` — ``OrgGuestAccess`` create. The guest is recorded by
  id, never email.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditLogEntry
from validibot.users.models import MemberInvite
from validibot.users.tests.factories import MembershipFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.workflows.models import OrgGuestAccess
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


def _clear_audit() -> None:
    """Drop every audit row.

    Used between arrange and act so fixture creation (which now emits its
    own audit entries) can't contaminate the assertion.
    """

    AuditLogEntry.objects.all().delete()


class MembershipLifecycleAuditTests(TestCase):
    """A member being removed from an org must leave a trail."""

    def test_membership_delete_records_member_removed(self) -> None:
        """Deleting a ``Membership`` fires ``pre_delete`` → MEMBER_REMOVED.

        This is what makes both ``membership.delete()`` call sites
        (members + users views) auditable without touching either view.
        """

        membership = MembershipFactory()
        org = membership.org
        _clear_audit()

        membership.delete()

        entry = AuditLogEntry.objects.get(action=AuditAction.MEMBER_REMOVED.value)
        self.assertEqual(entry.org_id, org.pk)


class OrganizationLifecycleAuditTests(TestCase):
    """Org update / delete, filed under the org itself."""

    def test_org_rename_records_org_updated_with_diff(self) -> None:
        """Renaming an org is a whitelisted field change, so ORG_UPDATED
        carries the before/after ``name`` diff for the operator.
        """

        org = OrganizationFactory(name="Before")
        _clear_audit()

        org.name = "After"
        org.save()

        entry = AuditLogEntry.objects.get(action=AuditAction.ORG_UPDATED.value)
        # ``org_resolver=lambda o: o`` files the entry under the org itself.
        self.assertEqual(entry.org_id, org.pk)
        self.assertEqual(entry.changes["name"]["before"], "Before")
        self.assertEqual(entry.changes["name"]["after"], "After")

    def test_org_delete_records_org_deleted(self) -> None:
        """Deleting an org emits ORG_DELETED so the teardown is auditable."""

        org = OrganizationFactory()
        _clear_audit()

        org.delete()

        self.assertTrue(
            AuditLogEntry.objects.filter(
                action=AuditAction.ORG_DELETED.value,
            ).exists(),
        )


class WorkflowStepAuditTests(TestCase):
    """Adding a step is a 'validator added' config change."""

    def test_step_create_records_validator_added_scoped_to_workflow_org(
        self,
    ) -> None:
        """A new ``WorkflowStep`` emits VALIDATOR_ADDED, and the owning org
        is resolved through ``step.workflow.org`` (not a direct ``.org``,
        which the step doesn't have) so it lands in the right org's view.
        """

        workflow = WorkflowFactory()
        _clear_audit()

        WorkflowStepFactory(workflow=workflow)

        entry = AuditLogEntry.objects.filter(
            action=AuditAction.VALIDATOR_ADDED.value,
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.org_id, workflow.org_id)


class MemberInviteAuditTests(TestCase):
    """Inviting a member is auditable — without storing the invitee email."""

    INVITEE_EMAIL = "invitee@external.example"

    def test_member_invite_records_invited_without_email(self) -> None:
        """Creating a ``MemberInvite`` emits MEMBER_INVITED with the roles
        and status in metadata, but the invitee's email — third-party PII —
        must not appear anywhere on the immutable entry.
        """

        org = OrganizationFactory()
        inviter = UserFactory()
        _clear_audit()

        MemberInvite.objects.create(
            org=org,
            inviter=inviter,
            invitee_email=self.INVITEE_EMAIL,
            roles=["author"],
            expires_at=timezone.now() + timedelta(days=7),
        )

        entry = AuditLogEntry.objects.get(action=AuditAction.MEMBER_INVITED.value)
        self.assertEqual(entry.org_id, org.pk)
        self.assertEqual(entry.metadata["roles"], ["author"])
        # The invitee email must never be captured.
        blob = f"{entry.changes}{entry.metadata}{entry.target_repr}"
        self.assertNotIn(self.INVITEE_EMAIL, blob)
        self.assertNotIn("@", blob)


class OrgGuestAccessAuditTests(TestCase):
    """Granting org-wide guest access is auditable by guest id, not email."""

    def test_org_guest_access_create_records_guest_granted(self) -> None:
        """Creating an ``OrgGuestAccess`` row emits GUEST_GRANTED. The guest
        is recorded by id in metadata; no email is captured.
        """

        org = OrganizationFactory()
        guest = UserFactory()
        granter = UserFactory()
        _clear_audit()

        access = OrgGuestAccess.objects.create(
            user=guest,
            org=org,
            granted_by=granter,
        )

        entry = AuditLogEntry.objects.get(action=AuditAction.GUEST_GRANTED.value)
        self.assertEqual(entry.org_id, org.pk)
        self.assertEqual(entry.target_id, str(access.pk))
        self.assertEqual(entry.metadata["guest_user_id"], guest.pk)
        blob = f"{entry.changes}{entry.metadata}{entry.target_repr}"
        self.assertNotIn("@", blob)

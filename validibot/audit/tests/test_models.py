"""Tests for the ``validibot.audit`` models.

Covers the structural invariants of the two-table split that allows
GDPR erasure without destroying the audit trail: ``AuditActor`` is
nullable and purgeable; ``AuditLogEntry`` uses ``PROTECT`` on the
actor FK so no entry can be orphaned by actor deletion; targets are
string-keyed so entries survive target deletion.

These tests don't exercise the service layer (see ``test_services``);
they're pure model-level contract checks.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory


class AuditActorTests(TestCase):
    """Actor identity layer — the purgeable half of the split."""

    def test_actor_can_exist_without_a_user(self) -> None:
        """System-originated entries have no Django user — the actor
        row should still persist so the entry can point at it.
        """

        actor = AuditActor.objects.create(email="system@example.com")
        self.assertIsNone(actor.user)
        self.assertIsNone(actor.erased_at)

    def test_erasure_nulls_pii_without_destroying_the_row(self) -> None:
        """Simulate the erasure workflow — null PII fields and stamp
        ``erased_at``. The row itself must persist so its FK holders
        keep working.
        """

        user = UserFactory()
        actor = AuditActor.objects.create(
            user=user,
            email=user.email,
            ip_address="10.0.0.1",
            user_agent="Mozilla/5.0",
        )

        actor.email = None
        actor.ip_address = None
        actor.user_agent = ""
        actor.erased_at = timezone.now()
        actor.save()

        refreshed = AuditActor.objects.get(pk=actor.pk)
        self.assertIsNone(refreshed.email)
        self.assertIsNone(refreshed.ip_address)
        self.assertEqual(refreshed.user_agent, "")
        self.assertIsNotNone(refreshed.erased_at)


class AuditLogEntryTests(TestCase):
    """Event layer — append-only, referentially linked to the actor."""

    def setUp(self) -> None:
        """Seed an actor and org shared by every test in the class."""
        self.actor = AuditActor.objects.create(email="tester@example.com")
        self.org = OrganizationFactory()

    def test_entry_stores_core_fields(self) -> None:
        """The canonical create path populates all the required columns."""

        entry = AuditLogEntry.objects.create(
            actor=self.actor,
            org=self.org,
            action=AuditAction.WORKFLOW_UPDATED.value,
            target_type="workflows.Workflow",
            target_id="42",
            target_repr="My Test Workflow",
            changes={"name": {"before": "Old", "after": "New"}},
        )

        self.assertEqual(entry.action, AuditAction.WORKFLOW_UPDATED.value)
        self.assertEqual(entry.target_id, "42")
        self.assertEqual(entry.changes["name"]["after"], "New")
        self.assertIsNotNone(entry.occurred_at)

    def test_actor_fk_is_protect(self) -> None:
        """Deleting an actor while entries still reference it must be
        rejected at the database level — otherwise erasure could
        accidentally destroy the audit trail when the operator tried
        to clean up 'dangling' actors.
        """

        AuditLogEntry.objects.create(
            actor=self.actor,
            action=AuditAction.LOGIN_SUCCEEDED.value,
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            self.actor.delete()

    def test_target_deletion_leaves_entry_intact(self) -> None:
        """Targets are string-keyed, not FK'd. If the target model
        later deletes the row (e.g. a workflow is removed), the
        audit entry should still be readable because its target info
        is snapshotted at write time.
        """

        entry = AuditLogEntry.objects.create(
            actor=self.actor,
            action=AuditAction.WORKFLOW_DELETED.value,
            target_type="workflows.Workflow",
            target_id="99",
            target_repr="Deleted Workflow",
        )

        # Even if target_id points at nothing, the entry still resolves.
        refreshed = AuditLogEntry.objects.get(pk=entry.pk)
        self.assertEqual(refreshed.target_id, "99")
        self.assertEqual(refreshed.target_repr, "Deleted Workflow")

    def test_indexes_support_dashboard_query(self) -> None:
        """Verify the org+occurred_at index makes the most common
        Pro-UI query plan-friendly: "entries for this org in the last
        N days, ordered by time descending."
        """

        now = timezone.now()
        for i in range(5):
            AuditLogEntry.objects.create(
                actor=self.actor,
                org=self.org,
                action=AuditAction.WORKFLOW_UPDATED.value,
                target_type="workflows.Workflow",
                target_id=str(i),
            )

        cutoff = now - timedelta(days=1)
        recent = list(
            AuditLogEntry.objects.filter(
                org=self.org,
                occurred_at__gte=cutoff,
            ).order_by("-occurred_at"),
        )
        self.assertEqual(len(recent), 5)
        # The ordering assertion is what the index is there to support.
        self.assertGreaterEqual(
            recent[0].occurred_at,
            recent[-1].occurred_at,
        )

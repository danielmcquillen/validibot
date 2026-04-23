"""Tests for ``AuditLogService`` — the single write path.

Three invariants the service enforces, with one test per invariant:

1. Non-whitelisted fields become ``<redacted>`` in the ``changes`` blob.
2. A fresh ``AuditActor`` row is created per write (no reuse across
   calls) so the erasure workflow can operate on whole actor rows.
3. Targets can be supplied either as a Django model OR as explicit
   type/id/repr strings (the erasure workflow uses the latter when
   recording actions against already-deleted targets).

Also includes a dedicated security test: the ``APIKey.key`` secret
must never appear in the ``changes`` blob even if a careless caller
passes it. This is the regression guard for a real OWASP-grade data
leak.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry
from validibot.audit.services import REDACTED
from validibot.audit.services import ActorSpec
from validibot.audit.services import AuditLogService
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory


class AuditLogServiceBasicWriteTests(TestCase):
    """Happy-path: service wraps the DB writes and emits a log marker."""

    def test_record_creates_actor_and_entry_atomically(self) -> None:
        """A successful call produces exactly one actor row + one
        entry row, both persisted in the same transaction.
        """

        user = UserFactory()
        org = OrganizationFactory()

        before_actors = AuditActor.objects.count()
        before_entries = AuditLogEntry.objects.count()

        entry = AuditLogService.record(
            action=AuditAction.LOGIN_SUCCEEDED,
            actor=ActorSpec(user=user, ip_address="10.0.0.1"),
            org=org,
        )

        self.assertEqual(AuditActor.objects.count(), before_actors + 1)
        self.assertEqual(AuditLogEntry.objects.count(), before_entries + 1)
        self.assertEqual(entry.action, AuditAction.LOGIN_SUCCEEDED.value)
        self.assertEqual(entry.org_id, org.pk)
        # Actor row carries the user's email automatically when not
        # explicitly set.
        self.assertEqual(entry.actor.email, user.email)
        self.assertEqual(entry.actor.ip_address, "10.0.0.1")

    def test_each_record_creates_a_fresh_actor_row(self) -> None:
        """Actors are not reused across calls — that keeps the erasure
        workflow simple because it operates on whole actor rows.
        """

        user = UserFactory()
        entry1 = AuditLogService.record(
            action=AuditAction.LOGIN_SUCCEEDED,
            actor=ActorSpec(user=user),
        )
        entry2 = AuditLogService.record(
            action=AuditAction.LOGIN_SUCCEEDED,
            actor=ActorSpec(user=user),
        )

        self.assertNotEqual(entry1.actor_id, entry2.actor_id)


class TargetResolutionTests(TestCase):
    """Either a Django model or explicit type/id/repr strings is OK."""

    def test_target_from_django_model_instance(self) -> None:
        """Passing a model instance populates target_type/id/repr from it."""

        user = UserFactory()
        actor = ActorSpec(user=user)
        # A User instance serves as a convenient stand-in for any
        # model — the service only reads ``_meta.label``, ``pk``, and
        # ``str()``.
        entry = AuditLogService.record(
            action=AuditAction.ADMIN_OBJECT_CHANGED,
            actor=actor,
            target=user,
        )

        self.assertEqual(entry.target_type, "users.User")
        self.assertEqual(entry.target_id, str(user.pk))
        self.assertIn(str(user), entry.target_repr)

    def test_explicit_target_strings_take_precedence(self) -> None:
        """Explicit strings override anything the service could derive
        from a model instance. Needed by the erasure workflow when the
        target object is already gone.
        """

        user = UserFactory()
        entry = AuditLogService.record(
            action=AuditAction.WORKFLOW_DELETED,
            actor=ActorSpec(user=user),
            target_type="workflows.Workflow",
            target_id="gone-forever",
            target_repr="Deleted Workflow Name",
        )

        self.assertEqual(entry.target_type, "workflows.Workflow")
        self.assertEqual(entry.target_id, "gone-forever")
        self.assertEqual(entry.target_repr, "Deleted Workflow Name")


class FieldWhitelistingTests(TestCase):
    """Only fields in ``AUDITABLE_FIELDS`` are captured verbatim."""

    def test_whitelisted_field_passes_through(self) -> None:
        """``Workflow.name`` is whitelisted, so the diff appears in full."""

        user = UserFactory()
        entry = AuditLogService.record(
            action=AuditAction.WORKFLOW_UPDATED,
            actor=ActorSpec(user=user),
            target_type="workflows.Workflow",
            target_id="7",
            changes={"name": {"before": "Old", "after": "New"}},
        )

        self.assertEqual(
            entry.changes["name"],
            {"before": "Old", "after": "New"},
        )

    def test_non_whitelisted_field_is_redacted(self) -> None:
        """``Workflow.secret_notes`` is not whitelisted, so the diff
        surface records the fact of change without the value.
        """

        user = UserFactory()
        entry = AuditLogService.record(
            action=AuditAction.WORKFLOW_UPDATED,
            actor=ActorSpec(user=user),
            target_type="workflows.Workflow",
            target_id="7",
            changes={
                "name": {"before": "Old", "after": "New"},
                "secret_notes": {"before": "x", "after": "y"},
            },
        )

        self.assertEqual(
            entry.changes["name"],
            {"before": "Old", "after": "New"},
        )
        self.assertEqual(entry.changes["secret_notes"], REDACTED)

    def test_api_key_secret_is_never_captured(self) -> None:
        """Security regression guard. ``authtoken.Token`` is the model
        behind Validibot's "API key" surface; the ``key`` column IS
        the credential. If a caller ever passes ``key`` in the
        changes dict, the service MUST redact it — otherwise every
        audit entry for a token create/revoke would leak the raw
        secret.

        ``authtoken.Token`` has no whitelisted fields (see
        ``AUDITABLE_FIELDS`` in constants.py), so every entry in
        ``changes`` is redacted unconditionally. The audit trail
        still records the fact of creation/revocation via the
        dedicated action codes.
        """

        user = UserFactory()
        entry = AuditLogService.record(
            action=AuditAction.API_KEY_CREATED,
            actor=ActorSpec(user=user),
            target_type="authtoken.Token",
            target_id="api-key-123",
            changes={
                # The dangerous field — must be redacted.
                "key": {"before": None, "after": "super-secret-real-value"},
                # Even benign-looking fields get redacted because
                # authtoken.Token's whitelist is empty.
                "user_id": {"before": None, "after": user.pk},
            },
        )

        self.assertEqual(entry.changes["key"], REDACTED)
        self.assertEqual(entry.changes["user_id"], REDACTED)


class LogMarkerEmissionTests(TestCase):
    """The Cloud Logging marker must fire on every successful write."""

    def test_marker_is_emitted_via_structured_logger(self) -> None:
        """Without the marker, a DB-restored-from-backup scenario
        could silently lose audit entries. The stdout/print fallback
        is tested in a separate integration test where we capture
        real stdout; here we exercise the structured-logger path.
        """

        with patch("validibot.audit.services.logger") as mock_logger:
            AuditLogService.record(
                action=AuditAction.LOGIN_SUCCEEDED,
                actor=ActorSpec(email="marker@example.com"),
            )

        self.assertTrue(
            mock_logger.info.called,
            "AuditLogService should emit a structured log marker on every write.",
        )
        call_args = mock_logger.info.call_args
        self.assertEqual(call_args.args[0], "audit_entry")
        # PII must NOT be in the marker.
        extra = call_args.kwargs["extra"]
        self.assertNotIn("email", extra)
        self.assertNotIn("ip_address", extra)
        self.assertIn("audit_id", extra)
        self.assertIn("action", extra)

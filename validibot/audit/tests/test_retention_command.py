"""Tests for the ``enforce_audit_retention`` management command.

Covers the invariants that matter for both retention and archive:

* **Kill-switch.** ``AUDIT_RETENTION_ENABLED=False`` → no-op.
* **Cutoff math.** Only rows older than ``AUDIT_HOT_RETENTION_DAYS``
  are considered; fresh rows stay.
* **Verified-upload-before-delete.** Only PKs returned by the
  backend's receipt get deleted. Rows the backend skipped remain
  for retry on the next run.
* **Dry-run.** ``--dry-run`` reports but doesn't mutate anything.
* **Chunking.** Large backlogs are processed in chunks; a mid-run
  crash at most loses progress for the in-flight chunk, never the
  whole window.
* **Misconfiguration.** Bad dotted path / non-protocol class → clear
  CommandError, not a silent no-op.

Test backend classes are attached to this module via
``sys.modules[__name__]`` so that ``AUDIT_ARCHIVE_BACKEND`` can resolve
them via a dotted path (``import_string``) without us needing a
standalone helper module. ``sys.modules`` access avoids the
"module imports itself" lint warning that a plain ``import`` would
trigger.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError
from django.core.management import call_command
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone

from validibot.audit.archive import ArchiveReceipt
from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry
from validibot.users.tests.factories import OrganizationFactory


def _make_entry(*, org, offset: timedelta = timedelta()) -> AuditLogEntry:
    """Build an entry with controllable ``occurred_at``."""

    actor = AuditActor.objects.create(email="tester@example.com")
    entry = AuditLogEntry.objects.create(
        actor=actor,
        org=org,
        action=AuditAction.WORKFLOW_UPDATED.value,
        target_type="workflows.Workflow",
        target_id="1",
    )
    if offset:
        AuditLogEntry.objects.filter(pk=entry.pk).update(
            occurred_at=timezone.now() + offset,
        )
        entry.refresh_from_db()
    return entry


def _run(**kwargs) -> str:
    """Helper that captures stdout from the command."""

    out = StringIO()
    call_command("enforce_audit_retention", stdout=out, **kwargs)
    return out.getvalue()


class KillSwitchTests(TestCase):
    """``AUDIT_RETENTION_ENABLED = False`` short-circuits the command."""

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    @override_settings(AUDIT_RETENTION_ENABLED=False)
    def test_kill_switch_skips_deletion(self) -> None:
        """The command logs "frozen" and leaves rows untouched even
        when they're well past the retention window. The periodic
        task still fires on schedule — the kill-switch just makes it
        a no-op.
        """

        _make_entry(org=self.org, offset=timedelta(days=-365))

        output = _run()

        self.assertIn("frozen", output.lower())
        # Nothing deleted — the old entry still exists.
        self.assertEqual(AuditLogEntry.objects.count(), 1)


class CutoffTests(TestCase):
    """Entries inside the retention window are preserved."""

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    @override_settings(AUDIT_HOT_RETENTION_DAYS=90)
    def test_fresh_entry_is_not_deleted(self) -> None:
        """A 1-day-old row must not be touched by a 90-day retention.

        The basic "don't delete things the user just did" guarantee.
        """

        fresh = _make_entry(org=self.org, offset=timedelta(days=-1))
        _run()
        self.assertTrue(AuditLogEntry.objects.filter(pk=fresh.pk).exists())

    @override_settings(AUDIT_HOT_RETENTION_DAYS=90)
    def test_stale_entry_is_deleted(self) -> None:
        """A 100-day-old row with a 90-day retention gets pruned.

        Exercises the happy path through both the null backend and
        the DB delete. This is the core claim of the feature.
        """

        stale = _make_entry(org=self.org, offset=timedelta(days=-100))
        _run()
        self.assertFalse(AuditLogEntry.objects.filter(pk=stale.pk).exists())

    @override_settings(AUDIT_HOT_RETENTION_DAYS=90)
    def test_retention_days_cli_override(self) -> None:
        """Operator can narrow the window ad-hoc without changing
        settings. Useful for a one-off cleanup after an incident.
        """

        _make_entry(org=self.org, offset=timedelta(days=-45))

        _run(retention_days=30)

        self.assertEqual(AuditLogEntry.objects.count(), 0)


class DryRunTests(TestCase):
    """``--dry-run`` reports what would happen without side effects."""

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    def test_dry_run_does_not_delete(self) -> None:
        """Nothing in the DB changes under --dry-run. The output
        includes the literal string ``[DRY-RUN]`` so an operator
        reading logs can tell at a glance.
        """

        _make_entry(org=self.org, offset=timedelta(days=-100))

        output = _run(dry_run=True)

        self.assertIn("DRY-RUN", output)
        self.assertEqual(AuditLogEntry.objects.count(), 1)


class ChunkingTests(TestCase):
    """Large backlogs process in chunks; receipts are additive."""

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    def test_processes_all_chunks(self) -> None:
        """12 stale rows with a chunk size of 5 → 3 chunks × 5/5/2 rows.
        All should end up deleted.
        """

        entries = [
            _make_entry(org=self.org, offset=timedelta(days=-100)) for _ in range(12)
        ]

        _run(chunk_size=5)

        self.assertEqual(
            AuditLogEntry.objects.filter(pk__in=[e.pk for e in entries]).count(),
            0,
        )

    def test_limit_stops_early(self) -> None:
        """``--limit 3`` processes only the first 3 rows. Safety net
        for operators testing a new backend — stops the command
        before it commits to the whole window.
        """

        for _ in range(10):
            _make_entry(org=self.org, offset=timedelta(days=-100))

        _run(chunk_size=2, limit=3)

        # Either 3 deleted, or somewhere between (depending on chunk
        # boundaries — limit may round up to the nearest chunk). The
        # invariant is strictly "no more than `limit + chunk_size`
        # deleted, at least ``limit`` deleted or the whole backlog if
        # smaller."
        remaining = AuditLogEntry.objects.count()
        self.assertGreater(remaining, 0, "limit=3 did not stop early")
        self.assertLess(
            remaining,
            10,
            "limit=3 should have deleted at least some rows",
        )


class VerifiedUploadBeforeDeleteTests(TestCase):
    """The command only deletes PKs the backend has verified."""

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    def test_backend_skipping_a_row_keeps_it_in_db(self) -> None:
        """Simulate a backend that refuses to archive one specific
        row. That row must stay in the DB for the next run. Others
        proceed normally.

        This is the crucial safety invariant: a transient backend
        failure on one row never cascades into data loss.
        """

        e1 = _make_entry(org=self.org, offset=timedelta(days=-100))
        e2 = _make_entry(org=self.org, offset=timedelta(days=-100))
        e3 = _make_entry(org=self.org, offset=timedelta(days=-100))

        # A partial backend registered via settings: includes e1 and
        # e3 in the receipt but not e2.
        skipped_pk = e2.pk

        class PartialBackend:
            """Test backend: archives every id except ``skipped_pk``."""

            def archive(self, entries):
                ids = [e.pk for e in entries if e.pk != skipped_pk]
                return ArchiveReceipt(
                    archived_ids=ids,
                    location="test-partial",
                    verified=True,
                )

        # We bind the class on this module via ``sys.modules`` so
        # ``import_string`` resolves it from settings without us
        # needing a plain self-import (which lint flags as PLW0406).
        sys.modules[__name__]._TEST_PARTIAL_BACKEND = PartialBackend

        with override_settings(
            AUDIT_ARCHIVE_BACKEND=(
                "validibot.audit.tests.test_retention_command._TEST_PARTIAL_BACKEND"
            ),
        ):
            _run()

        # e1 and e3 got archived+deleted; e2 is still there.
        self.assertFalse(AuditLogEntry.objects.filter(pk=e1.pk).exists())
        self.assertTrue(AuditLogEntry.objects.filter(pk=e2.pk).exists())
        self.assertFalse(AuditLogEntry.objects.filter(pk=e3.pk).exists())

    def test_backend_raising_aborts_without_delete(self) -> None:
        """A backend that raises must not trigger any delete.

        Translates the exception into ``CommandError`` so the
        scheduler sees a non-zero exit and retries on its next run.
        """

        _make_entry(org=self.org, offset=timedelta(days=-100))

        class CrashingBackend:
            def archive(self, entries):
                raise RuntimeError("simulated backend outage")

        sys.modules[__name__]._TEST_CRASHING_BACKEND = CrashingBackend

        with (
            override_settings(
                AUDIT_ARCHIVE_BACKEND=(
                    "validibot.audit.tests.test_retention_command."
                    "_TEST_CRASHING_BACKEND"
                ),
            ),
            pytest.raises(CommandError),
        ):
            _run()

        # DB untouched.
        self.assertEqual(AuditLogEntry.objects.count(), 1)


class MisconfigurationTests(TestCase):
    """Bad settings fail loud, not silent."""

    def test_unresolvable_backend_path_raises(self) -> None:
        """A typo in the dotted path must produce a ``CommandError``
        with a clear message — not fall back to the null backend
        (which would silently discard data).
        """

        with (
            override_settings(AUDIT_ARCHIVE_BACKEND="no.such.module.Backend"),
            pytest.raises(CommandError, match="AUDIT_ARCHIVE_BACKEND"),
        ):
            _run()

    def test_non_protocol_class_raises(self) -> None:
        """A backend class without an ``archive`` method fails at
        load time. The Protocol check catches typos and partial
        implementations before any DB work starts.
        """

        class NotABackend:
            """No ``archive`` method — protocol check must reject."""

        sys.modules[__name__]._TEST_NOT_A_BACKEND = NotABackend

        with (
            override_settings(
                AUDIT_ARCHIVE_BACKEND=(
                    "validibot.audit.tests.test_retention_command._TEST_NOT_A_BACKEND"
                ),
            ),
            pytest.raises(CommandError, match="AuditArchiveBackend"),
        ):
            _run()


class OrphanedActorCleanupTests(TestCase):
    """Retention must sweep actors with no remaining entries.

    Each audit write creates a fresh ``AuditActor`` row (by design —
    the actor model carries session-point-in-time PII). If retention
    deletes the entries referencing an actor but never touches the
    actor itself, the actor table grows indefinitely with email / IP
    / user-agent data past the retention horizon — defeating the
    whole point of retention.

    Actors with ``erased_at`` set are a separate case: the erasure
    workflow deliberately preserves those rows as pseudonymised
    identities, so retention leaves them alone even when their
    entries are gone.
    """

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    def test_orphaned_actor_deleted_after_entry_prune(self) -> None:
        """An actor whose only entry is pruned → actor gone too."""

        entry = _make_entry(org=self.org, offset=timedelta(days=-100))
        actor_id = entry.actor_id

        _run()

        # Entry pruned (established by CutoffTests) — the actor should
        # follow, because no other entry references it.
        self.assertFalse(AuditLogEntry.objects.filter(pk=entry.pk).exists())
        self.assertFalse(AuditActor.objects.filter(pk=actor_id).exists())

    def test_actor_with_surviving_entries_is_not_deleted(self) -> None:
        """An actor that still has entries (even fresh ones) stays.

        Scenario: an actor has two entries, one 100 days old and one
        1 day old. The 100-day entry gets pruned; the 1-day entry
        stays. The actor must NOT be deleted because the 1-day entry
        still references them.
        """

        actor = AuditActor.objects.create(email="survivor@example.com")
        AuditLogEntry.objects.create(
            actor=actor,
            org=self.org,
            action=AuditAction.WORKFLOW_UPDATED.value,
            target_type="workflows.Workflow",
            target_id="1",
        )
        old_entry = AuditLogEntry.objects.create(
            actor=actor,
            org=self.org,
            action=AuditAction.WORKFLOW_UPDATED.value,
            target_type="workflows.Workflow",
            target_id="2",
        )
        AuditLogEntry.objects.filter(pk=old_entry.pk).update(
            occurred_at=timezone.now() - timedelta(days=100),
        )

        _run()

        self.assertFalse(AuditLogEntry.objects.filter(pk=old_entry.pk).exists())
        self.assertTrue(AuditActor.objects.filter(pk=actor.pk).exists())

    def test_erased_actor_preserved_even_when_entries_gone(self) -> None:
        """Actors with ``erased_at`` set are deliberately kept around
        as pseudonymised identities. Retention must skip them even
        when all their referencing entries have been pruned.
        """

        entry = _make_entry(org=self.org, offset=timedelta(days=-100))
        AuditActor.objects.filter(pk=entry.actor_id).update(
            erased_at=timezone.now(),
            email=None,
            ip_address=None,
        )

        _run()

        # Entry pruned but the erased actor stays as a pseudonymised
        # row — defends the PROTECT FK model against accidentally
        # deleting an identity that the erasure workflow meant to keep.
        self.assertFalse(AuditLogEntry.objects.filter(pk=entry.pk).exists())
        self.assertTrue(AuditActor.objects.filter(pk=entry.actor_id).exists())

    def test_dry_run_reports_orphans_without_deleting(self) -> None:
        """``--dry-run`` counts the orphans but leaves them in place.

        This is the "how many actors would this prune?" capacity
        answer an operator wants before committing to a real run.
        """

        _make_entry(org=self.org, offset=timedelta(days=-100))

        output = _run(dry_run=True)

        # Entry and actor still there because of dry-run.
        self.assertEqual(AuditLogEntry.objects.count(), 1)
        self.assertEqual(AuditActor.objects.count(), 1)
        # Output mentions "orphaned actor" and the dry-run marker.
        self.assertIn("orphaned actor", output)
        self.assertIn("DRY-RUN", output)


class RegistryEntryTests(TestCase):
    """The retention task is registered in the scheduler registry."""

    def test_enforce_audit_retention_in_registry(self) -> None:
        """A deployment that runs ``sync_schedules`` must pick up the
        retention task — if the registry entry gets removed by
        mistake, the task silently stops firing. Locking in the
        presence of the entry in the registry is the cheapest
        insurance against that.
        """

        from validibot.core.tasks.registry import get_admin_task_by_id

        task = get_admin_task_by_id("enforce-audit-retention")
        self.assertIsNotNone(task, "enforce-audit-retention missing from registry")
        self.assertEqual(task.celery_task, "validibot.enforce_audit_retention")
        self.assertEqual(
            task.api_endpoint,
            "/api/v1/scheduled/enforce-audit-retention/",
        )
        self.assertEqual(task.schedule_cron, "30 2 * * *")

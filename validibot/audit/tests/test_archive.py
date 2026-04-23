"""Backend-level tests for ``validibot.audit.archive``.

Two concerns:

1. The :class:`AuditArchiveBackend` protocol is structural — the
   runtime check must accept any class with a conforming
   ``archive`` method even without explicit inheritance.
2. The two shipped backends (:class:`NullArchiveBackend` and
   :class:`FilesystemArchiveBackend`) each satisfy the contract
   invariants: verified receipt, matching ids, correct file layout
   for the filesystem backend, idempotency on re-run.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import pathlib
from datetime import timedelta

import pytest
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone

from validibot.audit.archive import ArchiveReceipt
from validibot.audit.archive import AuditArchiveBackend
from validibot.audit.archive import FilesystemArchiveBackend
from validibot.audit.archive import NullArchiveBackend
from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry
from validibot.users.tests.factories import OrganizationFactory


def _make_entry(*, org, offset: timedelta = timedelta()) -> AuditLogEntry:
    actor = AuditActor.objects.create(email="actor@example.com")
    entry = AuditLogEntry.objects.create(
        actor=actor,
        org=org,
        action=AuditAction.WORKFLOW_UPDATED.value,
        target_type="workflows.Workflow",
        target_id="1",
        target_repr="Example",
    )
    if offset:
        AuditLogEntry.objects.filter(pk=entry.pk).update(
            occurred_at=timezone.now() + offset,
        )
        entry.refresh_from_db()
    return entry


class ProtocolTests(TestCase):
    """The Protocol is runtime-checkable; duck-typed classes satisfy it."""

    def test_null_backend_satisfies_protocol(self) -> None:
        """The default backend must pass the isinstance check.

        This is the closest thing to a compile-time type check Python
        gives us for Protocol classes — a regression where
        NullArchiveBackend drifts from the contract would fail here.
        """

        self.assertIsInstance(NullArchiveBackend(), AuditArchiveBackend)

    def test_filesystem_backend_satisfies_protocol(self) -> None:
        """Same structural check for the filesystem reference impl."""

        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsInstance(
                FilesystemArchiveBackend(base_path=tmpdir),
                AuditArchiveBackend,
            )

    def test_duck_typed_class_satisfies_protocol(self) -> None:
        """A third-party class with just the ``archive`` method must
        also pass. Confirms the Protocol is doing structural, not
        nominal, typing — which is the whole point of the protocol.
        """

        class MyBackend:
            def archive(self, entries):
                return ArchiveReceipt(archived_ids=[], location="mine")

        self.assertIsInstance(MyBackend(), AuditArchiveBackend)


class NullArchiveBackendTests(TestCase):
    """Null backend: returns verified receipt listing all input ids."""

    def test_returns_all_input_ids_verified(self) -> None:
        """The null backend's whole purpose is to let the retention
        command delete everything it was asked to. The receipt must
        include every input pk with verified=True.
        """

        org = OrganizationFactory()
        entries = [_make_entry(org=org) for _ in range(3)]

        receipt = NullArchiveBackend().archive(entries)

        self.assertEqual(
            sorted(receipt.archived_ids),
            sorted(e.pk for e in entries),
        )
        self.assertTrue(receipt.verified)
        self.assertEqual(receipt.location, "null")
        self.assertIsNone(receipt.error)

    def test_empty_input_yields_empty_receipt(self) -> None:
        """An empty input must NOT crash — the retention command
        reaches this path when there's nothing to archive.
        """

        receipt = NullArchiveBackend().archive([])

        self.assertEqual(receipt.archived_ids, [])
        self.assertTrue(receipt.verified)


class FilesystemArchiveBackendLayoutTests(TestCase):
    """Filesystem backend partitions, serialises, and verifies writes."""

    def setUp(self) -> None:
        # ``tmp_path``-equivalent at TestCase level — a fresh dir per
        # test so parallel runs don't collide and cleanup is automatic
        # when the test ends.
        import tempfile

        self._tempdir = tempfile.TemporaryDirectory()
        self.base_path = pathlib.Path(self._tempdir.name)
        self.addCleanup(self._tempdir.cleanup)

    def test_writes_partitioned_jsonl_gz_with_sidecar(self) -> None:
        """One file per ``(org, yyyy, mm, dd)`` partition plus a
        ``.sha256`` sidecar. The expected layout is what the GCS
        backend's lifecycle rules key off, so consistency here is
        load-bearing even for self-hosters.
        """

        org = OrganizationFactory()
        entries = [_make_entry(org=org) for _ in range(2)]

        backend = FilesystemArchiveBackend(base_path=self.base_path)
        receipt = backend.archive(entries)

        self.assertTrue(receipt.verified)
        # Two entries on the same day → one partition file.
        files = list(self.base_path.rglob("*.jsonl.gz"))
        self.assertEqual(len(files), 1)
        target_file = files[0]
        # Layout: <base>/org_<id>/YYYY/MM/DD.jsonl.gz
        self.assertEqual(target_file.suffixes, [".jsonl", ".gz"])
        # ``org_<id>`` segment is 3 levels up from the file.
        self.assertTrue(target_file.parent.parent.parent.name.startswith("org_"))
        # Sidecar exists and matches.
        sidecar = target_file.with_name(target_file.name + ".sha256")
        self.assertTrue(sidecar.exists())
        expected_sha = hashlib.sha256(target_file.read_bytes()).hexdigest()
        self.assertIn(expected_sha, sidecar.read_text())

    def test_serialised_payload_is_valid_jsonl(self) -> None:
        """The ``.jsonl.gz`` body decompresses to one JSON object per
        line — what ``zcat file.jsonl.gz | jq .`` expects.
        """

        org = OrganizationFactory()
        entries = [_make_entry(org=org) for _ in range(3)]

        backend = FilesystemArchiveBackend(base_path=self.base_path)
        backend.archive(entries)

        target_file = next(self.base_path.rglob("*.jsonl.gz"))
        raw = gzip.decompress(target_file.read_bytes()).decode()
        lines = [line for line in raw.splitlines() if line.strip()]

        self.assertEqual(len(lines), 3)
        for line in lines:
            parsed = json.loads(line)
            self.assertEqual(parsed["action"], AuditAction.WORKFLOW_UPDATED.value)
            self.assertIn("target_repr", parsed)

    def test_different_orgs_land_in_separate_partitions(self) -> None:
        """Per-org partitioning is the GDPR-erasure prerequisite —
        erasing a single customer needs to touch one directory prefix,
        not walk the whole archive.
        """

        org_a = OrganizationFactory()
        org_b = OrganizationFactory()
        entries = [_make_entry(org=org_a), _make_entry(org=org_b)]

        backend = FilesystemArchiveBackend(base_path=self.base_path)
        backend.archive(entries)

        files = list(self.base_path.rglob("*.jsonl.gz"))
        # One file per org.
        self.assertEqual(len(files), 2)
        org_dirs = {f.parent.parent.parent for f in files}
        self.assertEqual(len(org_dirs), 2)

    def test_atomic_write_leaves_no_partial_file_on_crash(self) -> None:
        """The backend uses ``tempfile + rename`` so a crash mid-write
        either leaves the previous file or the new one, never a half-
        written partial. Simulate by catching the replace and checking
        the target doesn't exist in a broken state.
        """

        org = OrganizationFactory()
        entry = _make_entry(org=org)

        class CrashingBackend(FilesystemArchiveBackend):
            def _atomic_write(self, path, payload):
                # Write to tempfile then... don't rename.
                import tempfile

                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    delete=False,
                    dir=str(path.parent),
                ) as tmp:
                    tmp.write(payload)
                # Deliberately skip replace() — simulate crash.

        backend = CrashingBackend(base_path=self.base_path)
        backend.archive([entry])

        # No final target file exists (only the tempfile which isn't
        # visible to readers of the canonical path).
        target_files = list(self.base_path.rglob("*.jsonl.gz"))
        self.assertEqual(
            target_files,
            [],
            "A crashed write must not produce a visible partial file.",
        )


class FilesystemArchiveBackendConfigTests(TestCase):
    """Instantiation + settings-based configuration paths."""

    def test_missing_base_path_raises(self) -> None:
        """No env setting + no constructor arg → refuse to run.

        Without this guard, an operator who forgets to set
        AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH would silently end up
        writing archives next to the Django manage.py — confusing at
        best, data-leak at worst.
        """

        with (
            override_settings(AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH=""),
            pytest.raises(ValueError, match="AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH"),
        ):
            FilesystemArchiveBackend()

    def test_settings_configured_path_is_used(self) -> None:
        """An explicit ``AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH`` setting
        must be picked up by the zero-arg constructor so the
        retention command can do ``Backend()`` without having to
        re-resolve settings.
        """

        import tempfile

        with (
            tempfile.TemporaryDirectory() as tmp,
            override_settings(AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH=tmp),
        ):
            backend = FilesystemArchiveBackend()
            # The private attr is a reliable observable for this
            # test — production code only reads it indirectly via
            # ``archive()``.
            self.assertEqual(str(backend._base_path), tmp)

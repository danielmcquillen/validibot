"""Unit tests for :class:`GCSArchiveBackend`.

The backend talks to a real Google Cloud Storage bucket in
production, but here we mock ``google.cloud.storage.Client`` so
tests run offline with no GCP credentials. That lets CI prove the
protocol contract, the CMEK wiring, and the verify-on-read check
without coupling the test suite to an actual bucket.

### What the tests cover

* **Protocol conformance** — the backend is
  ``isinstance``-compatible with the community's
  :class:`AuditArchiveBackend` runtime-checkable ``Protocol``.
* **Happy path** — a group of entries uploads, verifies, and the
  receipt names every PK.
* **Verify-on-read mismatch** — if the re-read SHA differs from the
  uploaded SHA, the entries are *not* in the receipt. This is the
  invariant that protects a DB delete from following a silently-
  corrupted upload.
* **Upload exception is swallowed per-partition** — one org failing
  to upload must not break other orgs' partitions (a transient
  403 on one bucket shouldn't hold up the whole retention run).
* **CMEK key is applied** — when
  ``AUDIT_ARCHIVE_GCS_KMS_KEY`` is set, the upload sets
  ``blob.kms_key_name`` before the ``upload_from_string`` call.
* **Layout parity with the filesystem backend** — object names
  match ``org_<id>/YYYY/MM/DD.jsonl.gz`` exactly so operators who
  compare a GCS archive against a filesystem archive don't hit
  schema drift.
* **Config validation** — missing ``AUDIT_ARCHIVE_GCS_BUCKET``
  raises at construction.

### Test strategy

We patch at the *class* boundary (``storage.Client``) rather than
reaching into the module to replace the client attribute. That
makes the tests robust to refactoring and mirrors how the
community GCS-client tests are structured.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import timedelta
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone

from validibot.audit.archive import ArchiveReceipt
from validibot.audit.archive import AuditArchiveBackend
from validibot.audit.backends.gcs import GCSArchiveBackend
from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditActor
from validibot.audit.models import AuditLogEntry
from validibot.users.tests.factories import OrganizationFactory


def _make_entry(*, org, offset: timedelta = timedelta()) -> AuditLogEntry:
    actor = AuditActor.objects.create(email="t@example.com")
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


def _make_verifying_bucket(
    expected_bytes: bytes,
) -> tuple[MagicMock, dict[str, bytes], dict[str, MagicMock]]:
    """Build a bucket whose re-read returns exactly what was uploaded.

    The verify step is a ``download_as_bytes()`` followed by a
    SHA-256 compare. If we hand back the bytes we were given, the
    check must pass — that's the invariant the happy-path test is
    asserting.

    Returns the bucket, the ``uploaded`` dict (name → bytes), and a
    ``blobs`` dict (name → MagicMock blob) so tests can inspect
    per-blob state like ``kms_key_name``. We re-use a single blob
    mock per name so that any attribute set during the first
    ``bucket.blob(name)`` call (the upload) is visible on the second
    call (the verify).
    """

    bucket = MagicMock()
    uploaded: dict[str, bytes] = {}
    blobs: dict[str, MagicMock] = {}

    def make_blob(name):
        if name in blobs:
            return blobs[name]
        blob = MagicMock()

        # ``**_kwargs`` absorbs whatever keyword args the production
        # backend passes (``content_type``, ``if_generation_match``,
        # etc.) so the mock stays decoupled from the real call shape.
        def upload_from_string(data, **_kwargs):
            uploaded[name] = data

        blob.upload_from_string.side_effect = upload_from_string

        def download_as_bytes():
            return uploaded.get(name, expected_bytes)

        blob.download_as_bytes.side_effect = download_as_bytes
        blobs[name] = blob
        return blob

    bucket.blob.side_effect = make_blob
    return bucket, uploaded, blobs


# ── Protocol + construction ─────────────────────────────────────


class ProtocolConformanceTests(TestCase):
    """The runtime-checkable protocol accepts GCSArchiveBackend.

    Why this matters: the community retention command enforces
    protocol conformance at load-time via ``isinstance``. A refactor
    that accidentally drops the ``archive`` method would break the
    scheduled run but would only surface at runtime without this
    test.
    """

    @override_settings(AUDIT_ARCHIVE_GCS_BUCKET="test-bucket")
    def test_backend_satisfies_protocol(self) -> None:
        backend = GCSArchiveBackend()
        self.assertIsInstance(backend, AuditArchiveBackend)


class ConstructorTests(TestCase):
    """Configuration precedence + validation at init time."""

    def test_missing_bucket_raises(self) -> None:
        """No kwargs, no settings → loud error.

        Without this, the first scheduled run would fail at 02:30
        with a confusing log line. A startup-time raise makes the
        misconfiguration obvious during deploy.
        """

        with (
            override_settings(AUDIT_ARCHIVE_GCS_BUCKET=""),
            pytest.raises(ValueError, match="AUDIT_ARCHIVE_GCS_BUCKET"),
        ):
            GCSArchiveBackend()

    @override_settings(
        AUDIT_ARCHIVE_GCS_BUCKET="settings-bucket",
        AUDIT_ARCHIVE_GCS_PREFIX="from-settings",
        AUDIT_ARCHIVE_GCS_KMS_KEY="projects/p/locations/l/keyRings/r/cryptoKeys/k",
    )
    def test_reads_from_settings_by_default(self) -> None:
        """Zero-arg construction pulls every knob from Django settings
        — the retention command can't pass kwargs because it
        instantiates via a dotted-path string.
        """

        backend = GCSArchiveBackend()
        self.assertEqual(backend._bucket_name, "settings-bucket")
        self.assertEqual(backend._prefix, "from-settings")
        self.assertEqual(
            backend._kms_key_name,
            "projects/p/locations/l/keyRings/r/cryptoKeys/k",
        )

    def test_kwargs_override_settings(self) -> None:
        """Explicit kwargs win over settings. Lets unit tests
        exercise the class without monkey-patching global state.
        """

        with override_settings(AUDIT_ARCHIVE_GCS_BUCKET="settings-bucket"):
            backend = GCSArchiveBackend(bucket_name="explicit-bucket")
        self.assertEqual(backend._bucket_name, "explicit-bucket")


# ── Upload + verify happy path ──────────────────────────────────


class UploadAndVerifyTests(TestCase):
    """The main contract: upload + verify + receipt."""

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    @patch("validibot.audit.backends.gcs.storage.Client")
    def test_happy_path_returns_receipt_with_all_ids(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """All PKs in, all PKs in the receipt; uploads happened.

        The backend is considered correct when a round-trip through
        ``upload_from_string`` + ``download_as_bytes`` matches, which
        is what ``_make_verifying_bucket`` simulates. Any breakage
        in the verify path would leave the receipt empty and this
        test would fail loudly.
        """

        entries = [_make_entry(org=self.org) for _ in range(2)]
        bucket, uploaded, _ = _make_verifying_bucket(expected_bytes=b"")
        mock_client_cls.return_value.bucket.return_value = bucket

        backend = GCSArchiveBackend(bucket_name="test-bucket")
        receipt = backend.archive(entries)

        self.assertIsInstance(receipt, ArchiveReceipt)
        self.assertEqual(sorted(receipt.archived_ids), sorted(e.pk for e in entries))
        self.assertTrue(receipt.verified)
        # Two uploads: one main ``.jsonl.gz`` + one ``.sha256`` sidecar.
        self.assertEqual(len(uploaded), 2)

    @patch("validibot.audit.backends.gcs.storage.Client")
    def test_verify_mismatch_excludes_ids_from_receipt(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """When the re-read SHA doesn't match, the entries stay in
        the DB for the next run. This is the core safety invariant
        — a transient network corruption must never propagate to a
        DB delete.
        """

        entries = [_make_entry(org=self.org) for _ in range(3)]

        bucket = MagicMock()

        def make_blob(name):
            blob = MagicMock()
            # Main payload re-reads as corrupted bytes → SHA mismatch.
            blob.download_as_bytes.return_value = b"CORRUPTED"
            return blob

        bucket.blob.side_effect = make_blob
        mock_client_cls.return_value.bucket.return_value = bucket

        backend = GCSArchiveBackend(bucket_name="test-bucket")
        receipt = backend.archive(entries)

        self.assertEqual(receipt.archived_ids, [])
        self.assertTrue(receipt.verified)  # protocol field; partition-level failure

    @patch("validibot.audit.backends.gcs.storage.Client")
    def test_upload_exception_is_swallowed_per_partition(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """One bucket-level failure must not abort other orgs' work.

        We build entries for two orgs. The bucket raises on the
        second partition's first upload; the first partition
        succeeds normally. The receipt should name only the first
        org's ids.
        """

        org_a = self.org
        org_b = OrganizationFactory()
        entry_a = _make_entry(org=org_a)
        entry_b = _make_entry(org=org_b)

        bucket = MagicMock()
        uploaded: dict[str, bytes] = {}
        # Let the first org's 2 uploads (main + sidecar) succeed, then
        # fail on org_b's first upload.
        uploads_before_failure = 2
        upload_count = {"count": 0}

        def make_blob(name):
            blob = MagicMock()

            def upload_from_string(data, **_kwargs):
                # Fail the first upload that mentions org_b — that's
                # the main ``.jsonl.gz`` payload for the second
                # partition.
                if (
                    f"org_{org_b.pk}" in name
                    and upload_count["count"] >= uploads_before_failure
                ):
                    raise RuntimeError("simulated 403 on upload")
                uploaded[name] = data
                upload_count["count"] += 1

            blob.upload_from_string.side_effect = upload_from_string
            blob.download_as_bytes.side_effect = lambda: uploaded.get(name, b"")
            return blob

        bucket.blob.side_effect = make_blob
        mock_client_cls.return_value.bucket.return_value = bucket

        backend = GCSArchiveBackend(bucket_name="test-bucket")
        receipt = backend.archive([entry_a, entry_b])

        self.assertIn(entry_a.pk, receipt.archived_ids)
        self.assertNotIn(entry_b.pk, receipt.archived_ids)


# ── CMEK wiring ─────────────────────────────────────────────────


class CMEKWiringTests(TestCase):
    """``kms_key_name`` is applied to every uploaded blob when set."""

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    @patch("validibot.audit.backends.gcs.storage.Client")
    def test_kms_key_is_applied_when_set(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """Operators who configure a per-app CMEK key need the
        upload path to honour it. Without this assertion, a typo
        in the key setting would silently fall back to the bucket
        default and we'd only find out at audit time.
        """

        entry = _make_entry(org=self.org)
        key_name = "projects/p/locations/l/keyRings/r/cryptoKeys/audit"

        bucket, _, blobs = _make_verifying_bucket(expected_bytes=b"")
        mock_client_cls.return_value.bucket.return_value = bucket

        backend = GCSArchiveBackend(
            bucket_name="test-bucket",
            kms_key_name=key_name,
        )
        backend.archive([entry])

        # At least one blob was written (otherwise the test couldn't
        # prove anything). Every blob that had ``upload_from_string``
        # called must also have had ``kms_key_name`` set to the
        # configured key — otherwise the upload went out unencrypted
        # relative to our custom key.
        uploaded_blobs = [b for b in blobs.values() if b.upload_from_string.called]
        self.assertTrue(uploaded_blobs)
        for blob in uploaded_blobs:
            self.assertEqual(blob.kms_key_name, key_name)

    @patch("validibot.audit.backends.gcs.storage.Client")
    def test_no_kms_key_leaves_blob_default(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """When no key is configured, the backend lets GCS apply the
        bucket's default CMEK. We assert the code path doesn't
        touch ``blob.kms_key_name`` at all — anything else would
        silently override an operator's bucket-level setting.
        """

        entry = _make_entry(org=self.org)
        bucket, _, blobs = _make_verifying_bucket(expected_bytes=b"")
        mock_client_cls.return_value.bucket.return_value = bucket

        backend = GCSArchiveBackend(bucket_name="test-bucket", kms_key_name=None)
        backend.archive([entry])

        # The code path must not set ``kms_key_name`` at all. Any
        # assignment would cause the MagicMock's attribute to be a
        # concrete value; our assertion relies on that attribute
        # still being a MagicMock stub (a ``NonCallableMagicMock``
        # or similar) rather than a string.
        uploaded_blobs = [b for b in blobs.values() if b.upload_from_string.called]
        self.assertTrue(uploaded_blobs)
        for blob in uploaded_blobs:
            self.assertIsInstance(blob.kms_key_name, MagicMock)


# ── Layout parity with filesystem backend ──────────────────────


class LayoutParityTests(TestCase):
    """Object names match the filesystem backend's partition scheme.

    Operators switching between backends keep their downstream
    tooling working (``jq`` pipes, pandas glob expansion). Divergence
    here would silently break that promise.
    """

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    @patch("validibot.audit.backends.gcs.storage.Client")
    def test_object_name_matches_expected_format(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """Should produce ``<prefix>/org_<id>/YYYY/MM/DD<suffix>.jsonl.gz``
        where ``<suffix>`` is ``T<HHMMSS>Z-<4 hex>`` per the shared
        ``_unique_suffix()`` format.
        """

        # Anchor the entry's occurred_at at a specific date so the
        # object prefix is deterministic; the ``<suffix>`` portion
        # depends on wall-clock time + random hex and is matched with
        # a regex rather than a literal.
        entry = _make_entry(org=self.org)
        fixed_moment = timezone.now().replace(
            year=2026,
            month=4,
            day=22,
            hour=12,
            minute=0,
            second=0,
            microsecond=0,
        )
        AuditLogEntry.objects.filter(pk=entry.pk).update(occurred_at=fixed_moment)
        entry.refresh_from_db()

        bucket, uploaded, _ = _make_verifying_bucket(expected_bytes=b"")
        mock_client_cls.return_value.bucket.return_value = bucket

        backend = GCSArchiveBackend(
            bucket_name="test-bucket",
            prefix="audit",
        )
        backend.archive([entry])

        # One main upload (``.jsonl.gz``) and one sidecar (``.sha256``)
        # per partition. Match with regex because the suffix varies.
        import re

        expected = re.compile(
            rf"^audit/org_{self.org.pk}/2026/04/22T\d{{6}}Z-[0-9a-f]{{16}}\.jsonl\.gz$",
        )
        main_names = [name for name in uploaded if expected.match(name)]
        self.assertEqual(
            len(main_names),
            1,
            (
                f"Expected one main file matching {expected.pattern!r}; "
                f"got {sorted(uploaded)!r}"
            ),
        )
        self.assertIn(main_names[0] + ".sha256", uploaded)

    @patch("validibot.audit.backends.gcs.storage.Client")
    def test_same_day_chunks_do_not_overwrite(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """Critical regression: three archive() calls for the same
        ``(org, day)`` must produce three distinct GCS objects.

        Background: the community retention command chunks its work
        (default 500 rows per backend call). A backlog of many
        stale rows for one day drives N archive() calls against the
        same partition. With a single-object-name-per-day scheme,
        each call overwrites the previous GCS object — and the
        command has already deleted those rows from the DB by the
        time the next chunk arrives. Silent data loss.

        This test drives three archive() calls for the same org/day
        and asserts all three objects end up in the mock bucket
        with distinct names. A regression would cause the third
        call to overwrite the first two, failing either the count
        or the distinctness assertion.
        """

        bucket, uploaded, _ = _make_verifying_bucket(expected_bytes=b"")
        mock_client_cls.return_value.bucket.return_value = bucket

        backend = GCSArchiveBackend(bucket_name="test-bucket", prefix="audit")

        # Three archive() calls, each with its own entries, all for
        # the same org (setUp creates ``self.org``). The backend's
        # unique per-call suffix must make all three land in
        # different object names.
        backend.archive([_make_entry(org=self.org)])
        backend.archive([_make_entry(org=self.org)])
        backend.archive([_make_entry(org=self.org)])

        main_uploads = [name for name in uploaded if name.endswith(".jsonl.gz")]
        self.assertEqual(len(main_uploads), 3)
        self.assertEqual(
            len(set(main_uploads)),
            3,
            "Every archive() call must produce a distinct object name; "
            "overlap means same-day-overwrite has regressed.",
        )


# ── Payload shape ───────────────────────────────────────────────


class PayloadShapeTests(TestCase):
    """The gzipped JSONL body matches the documented row shape.

    Without this check, a refactor of ``_entry_to_archive_dict``
    could silently break cross-environment compatibility (filesystem
    archive from an on-prem Pro deployment vs. GCS archive from
    cloud) — they're meant to be interchangeable in downstream
    tooling.
    """

    def setUp(self) -> None:
        self.org = OrganizationFactory()

    @patch("validibot.audit.backends.gcs.storage.Client")
    def test_payload_decompresses_to_valid_jsonl(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        entries = [_make_entry(org=self.org) for _ in range(2)]

        captured: dict[str, bytes] = {}

        bucket = MagicMock()

        def make_blob(name):
            blob = MagicMock()

            def upload_from_string(data, **_kwargs):
                captured[name] = data

            blob.upload_from_string.side_effect = upload_from_string
            blob.download_as_bytes.side_effect = lambda: captured.get(name, b"")
            return blob

        bucket.blob.side_effect = make_blob
        mock_client_cls.return_value.bucket.return_value = bucket

        backend = GCSArchiveBackend(bucket_name="test-bucket")
        backend.archive(entries)

        main_name = next(name for name in captured if name.endswith(".jsonl.gz"))
        raw = gzip.decompress(captured[main_name]).decode()
        lines = [line for line in raw.splitlines() if line.strip()]

        self.assertEqual(len(lines), 2)
        for line in lines:
            parsed = json.loads(line)
            self.assertEqual(
                parsed["action"],
                AuditAction.WORKFLOW_UPDATED.value,
            )
            # Key fields the downstream tooling expects to find.
            for key in (
                "id",
                "occurred_at",
                "target_type",
                "target_id",
                "changes",
                "metadata",
            ):
                self.assertIn(key, parsed)

    @patch("validibot.audit.backends.gcs.storage.Client")
    def test_sidecar_contains_sha256_of_main_payload(
        self,
        mock_client_cls: MagicMock,
    ) -> None:
        """The ``.sha256`` sidecar must hold exactly the hex digest of
        the gzipped body. Auditors re-running the hash offline years
        later need this to prove integrity.
        """

        entry = _make_entry(org=self.org)

        captured: dict[str, bytes] = {}

        bucket = MagicMock()

        def make_blob(name):
            blob = MagicMock()

            def upload_from_string(data, **_kwargs):
                captured[name] = data

            blob.upload_from_string.side_effect = upload_from_string
            blob.download_as_bytes.side_effect = lambda: captured.get(name, b"")
            return blob

        bucket.blob.side_effect = make_blob
        mock_client_cls.return_value.bucket.return_value = bucket

        backend = GCSArchiveBackend(bucket_name="test-bucket")
        backend.archive([entry])

        main_name = next(name for name in captured if name.endswith(".jsonl.gz"))
        sidecar_name = main_name + ".sha256"
        expected = hashlib.sha256(captured[main_name]).hexdigest()
        self.assertIn(expected, captured[sidecar_name].decode())

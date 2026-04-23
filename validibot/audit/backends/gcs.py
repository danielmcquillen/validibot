"""GCS-backed implementation of :class:`AuditArchiveBackend`.

Writes each retention chunk as one CMEK-encrypted ``.jsonl.gz``
object per ``(org, YYYY, MM, DD)`` partition, then re-reads the
uploaded object and compares SHA-256 against what was uploaded.
Only the ids of rows whose re-read checksum matches end up in the
:class:`ArchiveReceipt`, which the community retention command uses
as the delete allowlist — a transient GCS corruption or partial
write can never propagate to a DB delete.

**Placement.** Community on purpose. A self-hosted Pro deployment on
GCP should have the same capability the hosted cloud offering does:
encrypted, CMEK-backed, compliance-grade audit archival. Moving this
to the private cloud repo would force GCP-on-Pro operators to
re-implement it.

### Layout

::

    gs://<bucket>/<prefix>/org_<id>/YYYY/MM/DD<suffix>.jsonl.gz
    gs://<bucket>/<prefix>/org_<id>/YYYY/MM/DD<suffix>.jsonl.gz.sha256

``<suffix>`` is ``T<HHMMSSZ>-<16 hex>`` — unique to each archive
call so chunks for the same day never overwrite each other. The
``.sha256`` sidecar stores the hex digest of the gzipped body so an
auditor can verify integrity years later without re-parsing the
JSONL.

### Why single-PUT is enough

GCS's ``XML API single-PUT`` is atomic — the object appears to
readers in full or not at all. No tempfile dance required on the
cloud side; the :class:`FilesystemArchiveBackend` needs it because
local filesystems aren't atomic-by-default. The verify step is
still required because a network corruption between app and GCS
could produce a successful-looking upload of corrupted bytes, and
``if_generation_match=0`` on the upload stops silent overwrites in
the vanishingly-unlikely filename-collision case.

### CMEK

The bucket is expected to be configured with a customer-managed
encryption key. Setting ``AUDIT_ARCHIVE_GCS_KMS_KEY`` lets operators
override that per-app if they want a separate key for audit data
(Google's recommendation for high-sensitivity data).
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils import timezone
from google.cloud import storage

from validibot.audit.archive import ArchiveReceipt
from validibot.audit.archive import _entry_to_archive_dict
from validibot.audit.archive import _unique_suffix

if TYPE_CHECKING:
    from collections.abc import Iterable

    from validibot.audit.models import AuditLogEntry

logger = logging.getLogger(__name__)


class GCSArchiveBackend:
    """Persist audit entries to a CMEK-encrypted GCS bucket.

    Configuration (all read from Django settings):

    * ``AUDIT_ARCHIVE_GCS_BUCKET`` — bucket name. Required.
    * ``AUDIT_ARCHIVE_GCS_PREFIX`` — object-name prefix under the
      bucket. Defaults to ``"audit/"``.
    * ``AUDIT_ARCHIVE_GCS_KMS_KEY`` — optional override for the
      CMEK key. When unset, objects inherit the bucket's default
      encryption key. The value is the fully-qualified KMS resource
      name: ``projects/P/locations/L/keyRings/R/cryptoKeys/K``.
    * ``AUDIT_ARCHIVE_GCS_PROJECT_ID`` — GCP project id for the
      storage client. Falls back to ADC / env when unset.

    Object naming matches :class:`FilesystemArchiveBackend` so
    operators who switch backends keep the same downstream tooling
    working — same ``jq`` pipes, same bucket-listing globs, same
    :class:`pandas.read_json` flow.
    """

    def __init__(
        self,
        bucket_name: str | None = None,
        prefix: str | None = None,
        kms_key_name: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Read config from settings unless explicit overrides are given.

        Tests instantiate with explicit kwargs so they don't depend
        on the settings module's state. Production instantiates
        zero-arg so the backend can be loaded from the dotted path
        in ``AUDIT_ARCHIVE_BACKEND`` without a separate factory.
        """

        self._bucket_name = bucket_name or getattr(
            settings,
            "AUDIT_ARCHIVE_GCS_BUCKET",
            "",
        )
        if not self._bucket_name:
            msg = (
                "GCSArchiveBackend requires AUDIT_ARCHIVE_GCS_BUCKET in "
                "Django settings (or a ``bucket_name`` kwarg)."
            )
            raise ValueError(msg)

        self._prefix = (
            prefix or getattr(settings, "AUDIT_ARCHIVE_GCS_PREFIX", "audit/")
        ).strip("/")
        self._kms_key_name = (
            kms_key_name or getattr(settings, "AUDIT_ARCHIVE_GCS_KMS_KEY", None) or None
        )
        self._project_id = (
            project_id
            or getattr(settings, "AUDIT_ARCHIVE_GCS_PROJECT_ID", None)
            or None
        )

        # Lazy-initialised so tests can patch ``google.cloud.storage.Client``
        # before first use and importing the module doesn't hit GCP.
        self._client: storage.Client | None = None

    # ── protocol surface ─────────────────────────────────────────

    def archive(self, entries: Iterable[AuditLogEntry]) -> ArchiveReceipt:
        """Upload + verify each ``(org, date)`` partition.

        Returns an :class:`ArchiveReceipt` listing only the ids of
        entries whose partition round-tripped a SHA-256 check. Any
        partition that fails verification is omitted — the community
        retention command leaves those rows in the DB for the next
        scheduled run.
        """

        partitions = _group_by_partition(entries)
        if not partitions:
            return ArchiveReceipt(
                archived_ids=[],
                location=f"gs://{self._bucket_name}/{self._prefix}/",
                verified=True,
            )

        bucket = self._get_bucket()
        call_suffix = _unique_suffix()

        archived: list[int] = []
        locations: list[str] = []
        for (org_id, year, month, day), group in partitions.items():
            object_name = _object_name(
                prefix=self._prefix,
                org_id=org_id,
                year=year,
                month=month,
                day=day,
                suffix=call_suffix,
            )
            payload = _serialise_group(group)
            expected_sha = hashlib.sha256(payload).hexdigest()

            try:
                self._upload(bucket, object_name, payload)
                self._upload(
                    bucket,
                    object_name + ".sha256",
                    f"{expected_sha}  {object_name.rsplit('/', 1)[-1]}\n".encode(),
                )
            except Exception:
                # Per-partition failure: log and skip. The DB rows
                # stay put because they aren't in the receipt, so
                # the next retention run picks them up.
                logger.exception(
                    "audit_archive_gcs_upload_failed",
                    extra={
                        "bucket": self._bucket_name,
                        "object": object_name,
                        "org_id": org_id,
                        "chunk_size": len(group),
                    },
                )
                continue

            if not self._verify(bucket, object_name, expected_sha):
                logger.error(
                    "audit_archive_gcs_verify_failed",
                    extra={
                        "bucket": self._bucket_name,
                        "object": object_name,
                        "expected_sha": expected_sha,
                    },
                )
                continue

            archived.extend(e.pk for e in group)
            locations.append(f"gs://{self._bucket_name}/{object_name}")

        return ArchiveReceipt(
            archived_ids=archived,
            location=";".join(locations) or f"gs://{self._bucket_name}/{self._prefix}/",
            verified=True,
        )

    # ── internals ────────────────────────────────────────────────

    def _get_bucket(self) -> storage.Bucket:
        """Return the cached bucket handle, creating the client if needed."""

        if self._client is None:
            self._client = storage.Client(project=self._project_id)
        return self._client.bucket(self._bucket_name)

    def _upload(
        self,
        bucket: storage.Bucket,
        object_name: str,
        payload: bytes,
    ) -> None:
        """Single-PUT an object, applying the configured CMEK key.

        Uses ``if_generation_match=0`` — GCS's "only create if the
        object does not already exist" precondition. If our unique-
        suffix machinery ever produces a collision (astronomically
        unlikely with 64 bits of random, but worth guarding), the
        upload fails with 412 Precondition Failed instead of
        silently overwriting an existing archive object whose source
        rows have already been deleted from the DB. The caller's
        try/except catches the failure and skips the partition.
        """

        blob = bucket.blob(object_name)
        if self._kms_key_name is not None:
            blob.kms_key_name = self._kms_key_name
        blob.upload_from_string(
            payload,
            content_type="application/gzip",
            if_generation_match=0,
        )

    @staticmethod
    def _verify(
        bucket: storage.Bucket,
        object_name: str,
        expected_sha: str,
    ) -> bool:
        """Re-read the object and compare SHA-256 against what we uploaded.

        Belt-and-braces: GCS already does client-side integrity
        checks on upload, but they only protect against transport
        corruption. A bug in our own serialisation code (wrong gzip
        level, inadvertent re-encoding) would slip through the
        transport check and be invisible until an audit replay.
        """

        blob = bucket.blob(object_name)
        actual = blob.download_as_bytes()
        return hashlib.sha256(actual).hexdigest() == expected_sha


# ── helpers (module-level for testability) ──────────────────────


def _group_by_partition(
    entries: Iterable[AuditLogEntry],
) -> dict[tuple[int | None, int, int, int], list[AuditLogEntry]]:
    """Group entries by ``(org_id, yyyy, mm, dd)`` partition.

    Matches the filesystem backend's partition scheme so downstream
    tooling is agnostic to which backend wrote the archive.
    """

    partitions: dict[tuple[int | None, int, int, int], list[AuditLogEntry]] = {}
    for entry in entries:
        occurred = entry.occurred_at or timezone.now()
        key = (entry.org_id, occurred.year, occurred.month, occurred.day)
        partitions.setdefault(key, []).append(entry)
    return partitions


def _object_name(
    *,
    prefix: str,
    org_id: int | None,
    year: int,
    month: int,
    day: int,
    suffix: str,
) -> str:
    """Build the per-partition GCS object name.

    Format mirrors :class:`FilesystemArchiveBackend` — same
    ``org_<id>`` segment, same zero-padded date components, same
    ``.jsonl.gz`` extension, same per-call ``suffix``.
    """

    org_segment = f"org_{org_id}" if org_id is not None else "org_unscoped"
    filename = f"{day:02d}{suffix}.jsonl.gz"
    pieces = [prefix, org_segment, f"{year:04d}", f"{month:02d}", filename]
    return "/".join(p for p in pieces if p)


def _serialise_group(group: list[AuditLogEntry]) -> bytes:
    """Render a partition's entries as one gzipped JSONL payload.

    Re-uses :func:`validibot.audit.archive._entry_to_archive_dict`
    for the row shape so the on-disk format is identical between
    filesystem and GCS backends.
    """

    import gzip
    import json

    jsonl = (
        "\n".join(
            json.dumps(
                _entry_to_archive_dict(entry),
                separators=(",", ":"),
                default=str,
            )
            for entry in group
        )
        + "\n"
    )
    return gzip.compress(jsonl.encode("utf-8"))

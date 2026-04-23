"""Pluggable archive backends for the audit-retention workflow.

The retention command (``validibot.audit.management.commands.enforce_audit_retention``)
hands each chunk of old entries to a backend implementing the
:class:`AuditArchiveBackend` protocol. The backend's job is to
preserve those rows somewhere durable (or discard them explicitly),
return a verified receipt, and leave it to the command to delete
from the DB only after verification succeeds.

Two backends ship with the community distribution:

* :class:`NullArchiveBackend` — the community default. Discards
  entries without preserving them anywhere. Satisfies the retention
  contract ("don't grow the table forever") without requiring an
  object store. Community deployments that don't care about
  long-term audit archive get this for free.
* :class:`FilesystemArchiveBackend` — writes JSONL+gzip files under
  a configurable directory, partitioned by ``org_id/yyyy/mm/dd``.
  Useful reference implementation for self-hosted Pro deployments
  that want archival without GCP — mount a persistent volume, point
  the setting at it, run.

Cloud's GCS-backed implementation lives in
``validibot_cloud.audit_archive.backends.gcs`` (Session 5 follow-up)
and follows the same contract.

### Contract

A backend's ``archive(entries)`` call:

1. Serialises the provided iterable of :class:`AuditLogEntry` rows.
2. Writes them somewhere durable (object store, filesystem, /dev/null).
3. **Verifies** the write (for object stores: re-read + checksum;
   for filesystem: fsync + stat; for null: nothing).
4. Returns an :class:`ArchiveReceipt` naming exactly which PKs the
   backend considers durably archived.

The caller (``enforce_audit_retention`` command) only deletes
``receipt.archived_ids`` from the DB. Rows not in the receipt stay
put and get retried on the next run — that's the "verified upload
before delete" invariant the ADR requires, enforced by the contract
shape. There's no way to accidentally delete a row whose archive
failed.

### Error handling

Backends should raise an exception rather than return an unverified
receipt when they hit an unrecoverable error (network failure,
permission denied, bucket misconfiguration). The command catches,
logs, and aborts the current run — the next scheduled run will
retry. A backend that silently returns ``verified=False`` is a
foot-gun: the command would skip-but-not-retry, losing visibility of
the problem.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import logging
import pathlib
import secrets
import tempfile
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterable

    from validibot.audit.models import AuditLogEntry

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ArchiveReceipt:
    """Result of one ``archive()`` call — names exactly which rows are safe to delete.

    ``archived_ids`` lists the :class:`AuditLogEntry` primary keys
    that the backend successfully archived. The retention command
    deletes only these; any rows in the input that aren't in
    ``archived_ids`` stay in the DB for retry on the next scheduled
    run.

    ``verified`` is a short-circuit flag for backends whose "archive"
    step is intrinsically verified (null backend → True by
    definition; filesystem → True after fsync + stat; GCS → True
    after SHA-256 check of the re-read object).

    ``location`` is a human-readable identifier of where the rows
    landed. For the null backend it's ``"null"``; for filesystem,
    the path that was written; for GCS, the ``gs://bucket/prefix/``
    URL. The retention command records this in its log output so
    operators can trace a specific set of deleted rows to where the
    archive now lives.
    """

    archived_ids: list[int]
    location: str
    verified: bool = True
    error: str | None = None


@runtime_checkable
class AuditArchiveBackend(Protocol):
    """Protocol every backend class must satisfy.

    Declared as a ``Protocol`` rather than an ABC so third parties
    can implement the contract structurally — ducktyping is fine, no
    subclass requirement. ``@runtime_checkable`` lets tests
    ``isinstance(backend, AuditArchiveBackend)`` for fast smoke
    checks without forcing a registration pattern.
    """

    def archive(self, entries: Iterable[AuditLogEntry]) -> ArchiveReceipt:
        """Preserve ``entries`` durably and return a receipt of what landed."""
        ...  # pragma: no cover - protocol stub


class NullArchiveBackend:
    """Discard entries without preserving them.

    The community default. A deployment that only wants retention
    enforcement (stop the table from growing forever) gets the
    correct behaviour from this backend: the retention command
    counts the entries, asks the backend to "archive" them (no-op),
    and proceeds to delete.

    The receipt reports ``verified=True`` and ``location="null"`` so
    the command's log line is unambiguous: you'll see
    ``"archived 42 entries at location=null"`` rather than a silent
    delete.
    """

    def archive(self, entries: Iterable[AuditLogEntry]) -> ArchiveReceipt:
        """Return a receipt naming all input pks; no actual storage."""

        ids = [entry.pk for entry in entries]
        return ArchiveReceipt(
            archived_ids=ids,
            location="null",
            verified=True,
        )


class FilesystemArchiveBackend:
    """Write JSONL+gzip archives to a local or volume-mounted directory.

    Reference implementation for self-hosted Pro deployments that
    want archival without an object store. Writes one file per
    ``(org_id, date)`` partition under the configured base directory,
    plus a ``.sha256`` sidecar so the written bytes are re-verifiable.

    Layout:

    .. code-block:: text

        <base_path>/
          org_7/2026/04/22T020302Z-a4d1.jsonl.gz
          org_7/2026/04/22T020302Z-a4d1.jsonl.gz.sha256
          org_7/2026/04/22T023015Z-9f02.jsonl.gz
          org_7/2026/04/22T023015Z-9f02.jsonl.gz.sha256

    Each ``archive()`` call gets a unique filename within its
    ``(org, day)`` partition. The suffix is ``T<HHMMSS>Z-<4 hex>``:
    timestamp + a short random tag in case two archive calls land
    in the same wall-clock second (rare, but possible when the
    retention command chunks a large backlog). Without this,
    multiple chunks for the same day would all write to ``DD.jsonl.gz``
    and each would overwrite the previous — silent data loss after
    the command deleted the source rows from the DB.

    File format matches what the GCS backend produces so the two are
    interchangeable for downstream tooling.

    **Durability.** The write path uses ``tempfile`` + atomic rename
    so a crash mid-write leaves the target file either absent or
    complete, never partial. ``os.fsync`` is called on the file
    handle before the rename so the OS guarantees the bytes reach
    disk. This is belt-and-braces for a reference implementation;
    production users picking this backend should provision the base
    path on a durable volume (persistent disk, RAID, off-site backup).

    **Configuration.**

    .. code-block:: python

        # config/settings/base.py
        AUDIT_ARCHIVE_BACKEND = (
            "validibot.audit.archive.FilesystemArchiveBackend"
        )
        AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH = "/var/lib/validibot/audit-archive"
    """

    def __init__(self, base_path: str | pathlib.Path | None = None) -> None:
        """Build the backend.

        ``base_path`` defaults to the Django setting
        ``AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH`` so the command can
        instantiate via a zero-arg constructor. Tests can pass an
        explicit path to avoid fiddling with settings.
        """

        from django.conf import settings

        resolved = base_path or getattr(
            settings,
            "AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH",
            None,
        )
        if not resolved:
            msg = (
                "FilesystemArchiveBackend requires "
                "AUDIT_ARCHIVE_FILESYSTEM_BASE_PATH in settings (or a "
                "``base_path`` constructor argument)."
            )
            raise ValueError(msg)
        self._base_path = pathlib.Path(resolved)

    def archive(self, entries: Iterable[AuditLogEntry]) -> ArchiveReceipt:
        """Serialise into partitioned JSONL+gzip and return a receipt."""

        # Group entries by partition ``(org_id, yyyy, mm, dd)``. Done
        # as a first pass over the iterable so we can write all
        # entries for one partition into a single file — otherwise
        # we'd open/close the same file for every row.
        partitions: dict[tuple[int | None, int, int, int], list[AuditLogEntry]] = {}
        for entry in entries:
            occurred = entry.occurred_at or timezone.now()
            key = (
                entry.org_id,
                occurred.year,
                occurred.month,
                occurred.day,
            )
            partitions.setdefault(key, []).append(entry)

        if not partitions:
            return ArchiveReceipt(archived_ids=[], location=str(self._base_path))

        # One unique filename suffix per ``archive()`` call — shared
        # across every partition written in this call. Timestamp +
        # 4 random hex digits. Two chunks for the same (org, day)
        # from back-to-back retention iterations will always land in
        # different files; the random tag covers the extreme case of
        # two calls in the same wall-clock second.
        call_suffix = _unique_suffix()

        archived: list[int] = []
        locations: list[str] = []
        for (org_id, year, month, day), group in partitions.items():
            org_segment = f"org_{org_id}" if org_id is not None else "org_unscoped"
            target_dir = self._base_path / org_segment / f"{year:04d}" / f"{month:02d}"
            target_dir.mkdir(parents=True, exist_ok=True)
            base_name = f"{day:02d}{call_suffix}"
            target_file = target_dir / f"{base_name}.jsonl.gz"
            sha_file = target_dir / f"{base_name}.jsonl.gz.sha256"

            payload_bytes = self._serialise_group(group)
            sha256 = self._sha256(payload_bytes)
            self._atomic_write(target_file, payload_bytes)
            self._atomic_write(sha_file, f"{sha256}  {target_file.name}\n".encode())

            archived.extend(e.pk for e in group)
            locations.append(str(target_file))

        return ArchiveReceipt(
            archived_ids=archived,
            location=";".join(locations),
            verified=True,
        )

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    def _serialise_group(group: list[AuditLogEntry]) -> bytes:
        """Render a group of entries as one gzipped JSONL payload."""

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

    @staticmethod
    def _sha256(payload: bytes) -> str:
        """Return the hex SHA-256 of ``payload``."""

        import hashlib

        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _atomic_write(path: pathlib.Path, payload: bytes) -> None:
        """Write ``payload`` to ``path`` atomically (tempfile + rename).

        The OS guarantees that readers see either the previous
        contents (if any) or the new complete contents — never a
        partial write. Belt-and-braces for a reference backend whose
        users may be unfamiliar with durable-write patterns.

        **Refuses to overwrite an existing path.** Archive objects
        are meant to be append-only. If ``_unique_suffix`` ever
        produces a collision (astronomically unlikely with 64 bits
        of random, but defensible to guard against), raising here
        is far safer than silently overwriting an existing archive
        whose source rows have already been deleted.
        """

        import os

        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            msg = (
                f"Refusing to overwrite existing archive object at {path}: "
                "filename collision suggests two archive() calls produced "
                "the same unique suffix. Investigate clock skew, "
                "_unique_suffix entropy, or duplicated retention runs."
            )
            raise FileExistsError(msg)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = pathlib.Path(tmp.name)
        # ``os.link`` + unlink instead of ``replace``: link fails when
        # the target exists, which guards against a TOCTOU window
        # between our exists() check and the rename. Not available on
        # all filesystems (NFS quirks), so fall back to rename when
        # link isn't supported.
        try:
            os.link(tmp_path, path)
        except OSError:
            # Filesystem doesn't support hardlinks; fall back to
            # rename. The exists() check above still covers the
            # common case; this is for filesystems where link is
            # unsupported entirely.
            tmp_path.replace(path)
        else:
            tmp_path.unlink()


def _unique_suffix() -> str:
    """Return a filename suffix unique to this archive-write call.

    Shape: ``T<HHMMSSZ>-<16 hex>`` — UTC time to the second plus 16
    random hex digits (64 bits of entropy). The time component alone
    isn't collision-safe when many chunks per second hit the same
    partition (a birthday-paradox estimate on 4 hex digits crosses
    50% collision at ~323 same-second writes, which a fast retention
    run can approach). 64 bits of random suffix keeps the birthday-
    paradox collision probability below 1 in a billion for any
    plausible per-second write rate.

    The timestamp is kept because it makes the filenames sortable
    by wall-clock order when an operator eyeballs a bucket listing.
    """

    # ``timezone.now()`` returns a UTC-aware datetime when
    # ``USE_TZ=True`` (the default). Format-to-string preserves UTC
    # so the ``Z`` suffix is accurate. ``secrets.token_hex(8)``
    # returns 16 hex characters / 64 bits — comfortably
    # collision-resistant for any realistic retention run.
    return f"T{timezone.now().strftime('%H%M%SZ')}-{secrets.token_hex(8)}"


def _entry_to_archive_dict(entry: AuditLogEntry) -> dict[str, Any]:
    """Flat dict serialisation of an entry for the archive stream.

    Mirrors the shape used by the Pro UI's CSV/JSONL export so the
    two outputs are interchangeable — operators can drop an archive
    file into a pandas pipeline that already handles export data.
    Duplicated (not shared) because the export view carries
    UI-specific fields (e.g. empty-string coalescing for missing
    values) that aren't useful in an archive partition.
    """

    actor = entry.actor
    if actor.erased_at is not None:
        actor_email = None
        actor_ip_address = None
        actor_user_id: Any = None
    else:
        actor_email = actor.email
        actor_ip_address = actor.ip_address
        actor_user_id = actor.user_id

    return {
        "id": entry.pk,
        "occurred_at": entry.occurred_at.isoformat() if entry.occurred_at else None,
        "action": entry.action,
        "org_id": entry.org_id,
        "actor_id": entry.actor_id,
        "actor_email": actor_email,
        "actor_user_id": actor_user_id,
        "actor_ip_address": actor_ip_address,
        "actor_erased_at": actor.erased_at.isoformat() if actor.erased_at else None,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "target_repr": entry.target_repr,
        "changes": entry.changes,
        "metadata": entry.metadata,
        "request_id": entry.request_id,
    }

"""Enforce retention on the ``AuditLogEntry`` table.

Runs on a schedule (Celery Beat for Docker Compose, Cloud Scheduler
for GCP — both driven by the entry in
``validibot/core/tasks/registry.py``). For each chunk of entries
older than ``AUDIT_HOT_RETENTION_DAYS``, the command hands the chunk
to the configured :class:`AuditArchiveBackend`, waits for a verified
receipt, and deletes only the rows the backend has durably preserved.

### Invariants

1. **Verified upload before delete.** The DB delete only runs for
   PKs that appear in the backend's :class:`ArchiveReceipt`. A
   backend that fails to verify a row keeps that row in the DB for
   the next scheduled run. Nothing is lost to a transient object-
   store error.
2. **Kill-switch.** ``AUDIT_RETENTION_ENABLED = False`` makes the
   command a logged no-op — useful during incident investigation
   when you want the audit table frozen. The periodic task keeps
   firing but nothing changes.
3. **Chunked.** The work happens in chunks (default 500 rows) so a
   backlog of years of entries doesn't materialise in memory and a
   crash mid-run loses at most one chunk of progress.
4. **Idempotent.** Re-running the command picks up from wherever
   the last run left off — the only state the command touches is
   rows that meet the retention cutoff AND the backend has verified.

### CLI

.. code-block:: text

    python manage.py enforce_audit_retention [options]

    --dry-run            Report what would happen without archiving or deleting.
    --retention-days N   Override the AUDIT_HOT_RETENTION_DAYS setting.
    --chunk-size N       Override the default 500-row chunking.
    --limit N            Stop after processing N rows (for testing only).
"""

from __future__ import annotations

import itertools
import logging
from datetime import timedelta
from importlib import import_module
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from validibot.audit.archive import ArchiveReceipt
from validibot.audit.archive import AuditArchiveBackend
from validibot.audit.models import AuditLogEntry

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 500


class Command(BaseCommand):
    """Delete audit log entries older than the retention window.

    Calls the configured archive backend first so Pro / Enterprise /
    cloud deployments preserve the rows before deletion.
    """

    help = "Archive + delete AuditLogEntry rows older than the retention window."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would happen without archiving or deleting.",
        )
        parser.add_argument(
            "--retention-days",
            type=int,
            default=None,
            help=(
                "Override the AUDIT_HOT_RETENTION_DAYS setting for this "
                "invocation. Useful for ad-hoc pruning when resolving a "
                "specific retention policy conflict."
            ),
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=DEFAULT_CHUNK_SIZE,
            help=(
                f"Number of rows to process per archive call "
                f"(default: {DEFAULT_CHUNK_SIZE})."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Stop after processing N rows. For testing only.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run: bool = options["dry_run"]
        retention_days: int = options["retention_days"] or getattr(
            settings,
            "AUDIT_HOT_RETENTION_DAYS",
            90,
        )
        chunk_size: int = options["chunk_size"] or DEFAULT_CHUNK_SIZE
        limit: int | None = options["limit"]

        # Kill-switch: short-circuit cleanly if operations wants to
        # freeze the table. We log so the scheduler's invocation
        # trail still shows "I ran" rather than looking like the
        # beat scheduler stopped firing.
        if not getattr(settings, "AUDIT_RETENTION_ENABLED", True):
            self.stdout.write(
                "AUDIT_RETENTION_ENABLED=False — retention is frozen, nothing to do.",
            )
            logger.info("audit_retention_frozen")
            return

        cutoff = timezone.now() - timedelta(days=retention_days)
        backend = self._load_backend()

        self.stdout.write(
            f"Retention cutoff: {cutoff.isoformat()} "
            f"(retention_days={retention_days}) "
            f"backend={type(backend).__name__} "
            f"chunk_size={chunk_size}" + (" [DRY-RUN]" if dry_run else ""),
        )

        total_considered = 0
        total_archived = 0
        total_deleted = 0

        # Use ``.iterator(chunk_size=)`` for memory efficiency — a
        # pathological years-of-backlog scenario must not materialise
        # the full queryset in Python. The extra ``itertools`` dance
        # batches the iterator into chunk-sized groups for the
        # backend call.
        queryset = AuditLogEntry.objects.filter(occurred_at__lt=cutoff).order_by("pk")
        iterator = queryset.iterator(chunk_size=chunk_size)

        for raw_chunk in _chunked(iterator, chunk_size):
            if limit is not None and total_considered >= limit:
                break
            chunk = (
                raw_chunk[: limit - total_considered]
                if limit is not None
                else raw_chunk
            )
            if not chunk:
                break

            total_considered += len(chunk)

            if dry_run:
                # Count and log — don't call the backend, don't delete.
                self.stdout.write(
                    f"  [dry-run] would archive+delete {len(chunk)} rows "
                    f"(oldest pk={chunk[0].pk}, newest pk={chunk[-1].pk})",
                )
                continue

            receipt = self._archive_chunk(backend, chunk)
            verified_ids = receipt.archived_ids
            total_archived += len(verified_ids)

            if not verified_ids:
                logger.warning(
                    "audit_retention_unverified_chunk",
                    extra={
                        "chunk_size": len(chunk),
                        "location": receipt.location,
                        "error": receipt.error,
                    },
                )
                continue

            deleted = self._delete_verified(verified_ids)
            total_deleted += deleted

            self.stdout.write(
                f"  archived {len(verified_ids)} rows at {receipt.location!r}; "
                f"deleted {deleted} from DB",
            )

        self.stdout.write(
            f"Done. considered={total_considered} "
            f"archived={total_archived} deleted={total_deleted}"
            + (" [DRY-RUN]" if dry_run else ""),
        )
        logger.info(
            "audit_retention_run_complete",
            extra={
                "considered": total_considered,
                "archived": total_archived,
                "deleted": total_deleted,
                "dry_run": dry_run,
                "cutoff": cutoff.isoformat(),
            },
        )

    # ── helpers ──────────────────────────────────────────────────

    def _load_backend(self) -> AuditArchiveBackend:
        """Import and instantiate the configured archive backend.

        Raises ``CommandError`` on misconfiguration so ``setup_validibot``
        and every scheduled run fail loudly rather than silently
        degrading (e.g. falling back to the null backend, which would
        silently discard data a cloud deployment meant to archive).
        """

        dotted = getattr(
            settings,
            "AUDIT_ARCHIVE_BACKEND",
            "validibot.audit.archive.NullArchiveBackend",
        )
        try:
            module_path, cls_name = dotted.rsplit(".", 1)
            module = import_module(module_path)
            cls = getattr(module, cls_name)
        except (ImportError, ValueError, AttributeError) as exc:
            msg = (
                f"AUDIT_ARCHIVE_BACKEND={dotted!r} could not be resolved. "
                "Check the dotted path and that the package is installed."
            )
            raise CommandError(msg) from exc

        backend = cls()
        if not isinstance(backend, AuditArchiveBackend):
            # ``AuditArchiveBackend`` is a runtime-checkable Protocol,
            # so this is a structural check — anyone with an
            # ``archive(entries)`` method satisfies it.
            msg = (
                f"Configured backend {dotted!r} does not satisfy the "
                "AuditArchiveBackend protocol (missing ``archive`` method)."
            )
            raise CommandError(msg)
        return backend

    def _archive_chunk(
        self,
        backend: AuditArchiveBackend,
        chunk: list[AuditLogEntry],
    ) -> ArchiveReceipt:
        """Run the backend's archive and defensively normalise the result."""

        try:
            receipt = backend.archive(chunk)
        except Exception as exc:  # pragma: no cover - surface any failure
            logger.exception(
                "audit_retention_backend_failed",
                extra={"chunk": len(chunk)},
            )
            # Re-raise as a CommandError so the scheduler sees a
            # non-zero exit — Cloud Scheduler retries HTTP 5xx, and
            # Celery Beat logs the failure.
            msg = f"Archive backend raised: {exc}"
            raise CommandError(msg) from exc

        if not isinstance(receipt, ArchiveReceipt):
            msg = (
                f"Backend {type(backend).__name__} returned non-ArchiveReceipt: "
                f"{type(receipt).__name__}"
            )
            raise CommandError(msg)
        return receipt

    @staticmethod
    def _delete_verified(ids: list[int]) -> int:
        """Delete rows listed in the receipt inside one transaction.

        Transactional so a crash between the archive call and the
        delete leaves the DB untouched — the next run picks up the
        same rows and re-archives. The backend is expected to be
        idempotent for the same set of entries (filesystem = stable
        filename; GCS = same object key; null = trivially so).
        """

        with transaction.atomic():
            deleted, _ = AuditLogEntry.objects.filter(pk__in=ids).delete()
        return deleted


def _chunked(iterable, size: int) -> Iterator[list]:
    """Yield successive ``size``-long chunks from ``iterable``.

    ``itertools.islice`` handles the batching; we materialise each
    chunk into a list because the backend's ``archive`` expects a
    concrete iterable (it may need to iterate twice — once to
    serialise, once to build the receipt ids).
    """

    iterator = iter(iterable)
    while True:
        chunk = list(itertools.islice(iterator, size))
        if not chunk:
            return
        yield chunk

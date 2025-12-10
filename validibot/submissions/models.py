from __future__ import annotations

import contextlib
import hashlib
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.base import File
from django.db import models
from django.db.models import Q
from django.utils.text import slugify
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from validibot.projects.models import Project
from validibot.submissions.constants import DataRetention
from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import Organization
from validibot.users.models import User
from validibot.workflows.models import Workflow

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile

logger = logging.getLogger(__name__)


def submission_input_file_upload_to(instance: Submission, filename: str) -> str:
    """
    Generate a unique upload path for submission files based on
    organization and project.
    """
    if not instance:
        err_msg = "Instance must be provided for upload path generation."
        raise ValueError(err_msg)
    if not instance.org_id:
        err_msg = "Submission must be associated with an organization."
        raise ValueError(err_msg)
    if not filename:
        err_msg = "Filename must be provided for upload path generation."
        raise ValueError(err_msg)

    org_part = f"o{instance.org_id}"
    proj_slug = instance.project.slug if instance.project_id else "none"
    proj_part = f"p{proj_slug[:16]}"
    user_part = f"u{instance.user_id}" if instance.user_id else "uanon"
    date_part = now().strftime("%Y%m%d")

    # Slugify filename for URL-safe storage paths while preserving extension.
    # Avoid trusting user-supplied characters; cap the stem length to keep paths sane.
    name_path = Path(filename)
    ext = name_path.suffix.lower()
    safe_stem = slugify(name_path.stem)[:50] or "file"
    safe_name = f"{safe_stem}{ext}"

    unique = uuid.uuid4().hex[:12]
    p = (
        f"submissions/{org_part}/{proj_part}/"
        f"{user_part}/{date_part}/{unique}_{safe_name}"
    )
    return p


class Submission(TimeStampedModel):
    """
    The actual content sent by a user for validation.
    If the content is large, we store it in a FileField (backed by S3 or similar).
    Otherwise, we store in a TextField in this model.
    """

    class Meta:
        indexes = [
            models.Index(
                fields=[
                    "org",
                    "project",
                    "workflow",
                    "created",
                ],
            ),
            models.Index(
                fields=[
                    "org",
                    "created",
                ],
            ),
        ]
        constraints = [
            # At least one of content or input_file (unless purged)
            models.CheckConstraint(
                name="submission_content_present",
                condition=(
                    Q(content_purged_at__isnull=False)  # Purged - no content needed
                    | Q(content__gt="")
                    | (Q(input_file__isnull=False) & ~Q(input_file=""))
                ),
            ),
            # Not both content and input_file
            models.CheckConstraint(
                name="submission_content_not_both",
                condition=~(
                    Q(content__gt="")
                    & (Q(input_file__isnull=False) & ~Q(input_file=""))
                ),
            ),
            # Purged submissions must have content cleared
            models.CheckConstraint(
                name="submission_purged_content_cleared",
                condition=(
                    Q(content_purged_at__isnull=True)
                    | (Q(content="") & Q(input_file=""))
                ),
            ),
        ]
        ordering = ["-created"]

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    name = models.CharField(
        max_length=256,
        blank=True,
        default="",
        help_text=_("Optional descriptive name."),
    )

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="submissions",
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="submissions",
        null=True,
        blank=True,
    )

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submissions",
    )

    # ACTUAL CONTENT PROVIDED BY USER
    # ~---------------------------------------------------------------

    # inline text for small JSON/XML/IDF
    content = models.TextField(
        blank=True,
        default="",
    )

    # file upload for larger content
    input_file = models.FileField(
        upload_to=submission_input_file_upload_to,
        help_text=_("The file to validate, e.g. IDF, JSON, XML, etc."),
        null=True,
        blank=True,
    )

    # ~---------------------------------------------------------------

    # Information about that user content ...

    file_type = models.CharField(
        max_length=64,
        choices=SubmissionFileType.choices,
    )

    original_filename = models.CharField(
        max_length=512,
        blank=True,
        default="",
    )

    size_bytes = models.BigIntegerField(default=0)

    checksum_sha256 = models.CharField(
        max_length=64,
        blank=True,
        default="",
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
    )

    # Info about how/why this submission was created

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.PROTECT,
        related_name="submissions",
        help_text=_("Workflow *version* to run."),
    )

    latest_run = models.OneToOneField(
        "validations.ValidationRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # Retention fields
    # ~---------------------------------------------------------------

    retention_policy = models.CharField(
        max_length=32,
        choices=DataRetention.choices,
        default=DataRetention.DO_NOT_STORE,
        help_text=_("Snapshot of workflow's retention policy at submission time."),
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "When content should be purged (null = already purged or DO_NOT_STORE)."
        ),
    )

    content_purged_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When content was purged (for audit trail)."),
    )

    # ~---------------------------------------------------------------

    # Methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def set_content(
        self,
        inline_text: str | None = None,
        uploaded_file: UploadedFile | File | None = None,
        filename: str | None = None,
        inline_max_bytes: int | None = None,
        file_type: str | None = None,
    ):
        """
        Take the content provided by the user in the POST request to start
        a validation run. Store that content either inline (if small enough)
        or in the FileField.

        IMPORTANT: This method does NOT call self.save(). The caller should
        call save() after setting any other fields.

        Args:
            inline_text (str | None, optional):
            uploaded_file (UploadedFile | File | None, optional):
            filename (str | None, optional):
            inline_max (int | None, optional):

        Raises:
            ValueError: _description_
        """

        inline_max_bytes = inline_max_bytes or int(
            getattr(settings, "SUBMISSION_INLINE_MAX_BYTES", 256 * 1024),
        )

        # Exactly one input
        if (inline_text is not None) and (uploaded_file is not None):
            raise ValueError(_("Cannot provide both inline_text and uploaded_file."))
        if (inline_text is None) and (uploaded_file is None):
            raise ValueError(_("Must provide either inline_text or uploaded_file."))

        # INLINE PATH
        if inline_text is not None:
            if not inline_text.strip():
                raise ValueError(_("inline_text cannot be empty."))
            data = inline_text.encode("utf-8")
            self.size_bytes = len(data)
            self.original_filename = filename or self.original_filename or "inline.txt"
            self.checksum_sha256 = self._compute_checksum(data)  # cheap, keep it
            if self.size_bytes <= inline_max_bytes:
                # store inline, delete any prior file
                self.content = inline_text
                if self.input_file:
                    with contextlib.suppress(Exception):
                        self.input_file.delete(save=False)
                self.input_file = None
            else:
                # spill to file storage
                self.content = ""
                if self.input_file:
                    with contextlib.suppress(Exception):
                        self.input_file.delete(save=False)
                final_name = Path(self.original_filename).name
                self.input_file.save(
                    final_name,
                    ContentFile(data),
                    save=False,
                )
                self.original_filename = final_name

        # UPLOAD PATH
        if uploaded_file is not None:
            final_name = filename or getattr(uploaded_file, "name", "") or "upload"
            final_name = Path(final_name).name
            # enforce XOR and delete any prior file to avoid orphans
            self.content = ""
            if self.input_file:
                with contextlib.suppress(Exception):
                    self.input_file.delete(save=False)

            # ensure at start then save in one pass
            with contextlib.suppress(Exception):
                uploaded_file.seek(0)
            self.input_file.save(final_name, uploaded_file, save=False)

            self.original_filename = final_name
            self.size_bytes = getattr(uploaded_file, "size", 0)
            if not self.size_bytes:
                # after save(), storage knows the size
                with contextlib.suppress(Exception):
                    self.size_bytes = self.input_file.size or 0

            # leave checksum blank; save() will backfill it efficiently
            self.checksum_sha256 = ""

        # File type detection (respect explicit valid value)
        if not file_type or file_type not in SubmissionFileType.values:
            file_type = detect_file_type(
                filename=self.original_filename or filename,
                text=inline_text if inline_text is not None else None,
            )
        self.file_type = file_type

        return True  # caller still does self.save()

    def get_content(self) -> str:
        """
        Retrieve the actual content of this submission, whether stored
        inline or in the FileField.

        Returns empty string if content has been purged.

        Returns:
            str: The content as a string, or empty string if purged/unavailable.
        """
        if self.content_purged_at:
            return ""  # Content has been purged
        if self.content:
            return self.content
        if self.input_file:
            try:
                with self.input_file.open("rb") as fh:
                    with contextlib.suppress(Exception):
                        fh.seek(0)
                    data = fh.read()
            except Exception:
                return ""
            return (
                data.decode("utf-8", errors="replace")
                if isinstance(data, bytes)
                else str(data)
            )
        return ""

    @property
    def is_content_available(self) -> bool:
        """Check if content is still available (not purged)."""
        return self.content_purged_at is None

    def clean(self, *args, **kwargs):
        errors = {}

        # Require same-org relationships (DB can't enforce this natively)
        if self.project_id and self.project.org_id != self.org_id:
            errors["project"] = _("Project must belong to the same organization.")
        if self.workflow_id and self.workflow.org_id != self.org_id:
            errors["workflow"] = _("Workflow must belong to the same organization.")
        if errors:
            raise ValidationError(errors)

        if self.user and self.user.orgs.filter(id=self.org_id).exists() is False:
            errors["user"] = _("User must belong to the same organization.")

        # Content presence: require exactly one of (content, input_file)
        # unless content has been purged
        if not self.content_purged_at:
            has_doc = bool(self.content)
            has_file = bool(self.input_file)
            if not (has_doc ^ has_file):
                errors["content"] = _("Provide exactly one of content or input_file.")

        if errors:
            raise ValidationError(errors)

        super().clean()

    def save(self, *args, **kwargs):
        # Backfill checksum for stored files with no checksum
        if self.input_file and not self.checksum_sha256:
            try:
                with self.input_file.open("rb"):
                    self.checksum_sha256 = self._compute_checksum_filelike(
                        self.input_file,
                    )
            except Exception:
                logger.exception(
                    "Failed to compute checksum for submission",
                    extra={"id": self.id},
                )

        # Ensure file_type is set
        if not self.file_type:
            self.file_type = detect_file_type(
                filename=self.original_filename
                or getattr(self.input_file, "name", None),
                text=self.content or None,
            )

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name or f"Submission {self.id}"

    def _compute_checksum_filelike(self, f, chunk_size=1024 * 1024) -> str:
        h = hashlib.sha256()
        can_seek = True
        try:
            pos = f.tell()
        except Exception:
            can_seek = False
            pos = None
        if can_seek:
            with contextlib.suppress(Exception):
                f.seek(0)
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            h.update(chunk)
        if can_seek and pos is not None:
            with contextlib.suppress(Exception):
                f.seek(pos)
        return h.hexdigest()

    def _compute_checksum(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def purge_content(self) -> None:
        """
        Remove submission content while preserving the record and metadata.

        This method is idempotent - calling it on an already-purged submission
        is a no-op.

        Keeps: id, checksum_sha256, original_filename, size_bytes, file_type,
               metadata, created, retention_policy
        Clears: content, input_file
        Sets: content_purged_at
        Also cleans up: GCS execution bundle folders for all related runs

        Raises:
            Exception: If file deletion fails (caller should handle retry)
        """
        if self.content_purged_at:
            return  # Already purged (idempotent)

        # Delete file from storage
        if self.input_file:
            try:
                self.input_file.delete(save=False)
            except Exception:
                logger.exception(
                    "Failed to delete submission file",
                    extra={"id": str(self.id)},
                )
                raise

        # Delete execution bundle folders for all runs
        for run in self.runs.all():
            try:
                _delete_execution_bundle(run)
            except Exception:
                logger.exception(
                    "Failed to delete execution bundle",
                    extra={"submission_id": str(self.id), "run_id": str(run.id)},
                )
                # Continue with other runs - don't fail entire purge

        # Clear content
        self.content = ""
        self.input_file = None
        self.content_purged_at = now()
        self.expires_at = None  # No longer pending
        self.save(
            update_fields=["content", "input_file", "content_purged_at", "expires_at"],
        )

        logger.info(
            "Purged submission content",
            extra={
                "submission_id": str(self.id),
                "retention_policy": self.retention_policy,
            },
        )


def _delete_execution_bundle(run) -> None:
    """
    Delete the GCS execution bundle folder for a validation run.

    Uses prefix deletion to remove all objects under the run's folder.
    This function is called during submission content purge to remove
    all associated files from cloud storage.

    Args:
        run: ValidationRun instance with summary containing execution_bundle_uri

    Note:
        - Safe to call if bundle doesn't exist (no-op)
        - Logs but doesn't raise on individual blob deletion failures
    """
    from urllib.parse import urlparse

    if not run.summary:
        return

    # Get execution bundle URI from run summary (set by launcher)
    bundle_uri = run.summary.get("execution_bundle_uri")
    if not bundle_uri or not bundle_uri.startswith("gs://"):
        return

    # Parse bucket and prefix from URI
    # gs://bucket/runs/org_id/run_id/ -> bucket, runs/org_id/run_id/
    parsed = urlparse(bundle_uri)
    bucket_name = parsed.netloc
    prefix = parsed.path.lstrip("/")

    try:
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blobs = list(bucket.list_blobs(prefix=prefix))

        for blob in blobs:
            try:
                blob.delete()
                logger.debug("Deleted GCS object: %s", blob.name)
            except Exception:
                logger.exception("Failed to delete GCS object: %s", blob.name)
                raise

        if blobs:
            logger.info(
                "Deleted execution bundle",
                extra={
                    "run_id": str(run.id),
                    "bundle_uri": bundle_uri,
                    "objects_deleted": len(blobs),
                },
            )
    except ImportError:
        # google-cloud-storage not installed (local dev without GCS)
        logger.warning(
            "google-cloud-storage not available, skipping execution bundle cleanup",
            extra={"run_id": str(run.id), "bundle_uri": bundle_uri},
        )
    except Exception:
        logger.exception(
            "Failed to delete execution bundle",
            extra={"run_id": str(run.id), "bundle_uri": bundle_uri},
        )
        raise


def detect_file_type(
    *,
    filename: str | None = None,
    text: str | None = None,
) -> str:
    name = (filename or "").lower()
    if name.endswith((".json", ".epjson")):
        return SubmissionFileType.JSON
    if name.endswith(".xml"):
        return SubmissionFileType.XML
    if name.endswith((".yaml", ".yml")):
        return SubmissionFileType.YAML
    if name.endswith(".idf") or "energyplus" in name:
        return SubmissionFileType.TEXT
    if text:
        s = text.lstrip()
        if s.startswith(("{", "[")):
            return SubmissionFileType.JSON
        if s.startswith("<"):
            return SubmissionFileType.XML
        if s.startswith(("---", "- ")):
            return SubmissionFileType.YAML
    return SubmissionFileType.UNKNOWN  # default fallback


class PurgeRetry(models.Model):
    """
    Track submissions that failed to purge and need retry.

    When a purge operation fails (e.g., GCS unavailable, file locked),
    we record it here for later retry. A scheduled job processes
    these records and attempts to complete the purge.

    This ensures data retention policies are eventually enforced
    even when transient failures occur.
    """

    MAX_ATTEMPTS = 5
    RETRY_DELAYS = [60, 300, 3600, 21600, 86400]  # 1m, 5m, 1h, 6h, 24h

    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        related_name="purge_retries",
        help_text=_("Submission that failed to purge."),
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text=_("When the initial purge failure occurred."),
    )

    last_attempt_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the last retry attempt was made."),
    )

    next_retry_at = models.DateTimeField(
        db_index=True,
        help_text=_("When to attempt the next retry."),
    )

    attempt_count = models.PositiveIntegerField(
        default=0,
        help_text=_("Number of retry attempts made."),
    )

    last_error = models.TextField(
        blank=True,
        default="",
        help_text=_("Error message from the last failed attempt."),
    )

    class Meta:
        verbose_name_plural = "Purge retries"
        indexes = [
            models.Index(fields=["next_retry_at", "attempt_count"]),
        ]

    def __str__(self) -> str:
        return f"PurgeRetry({self.submission_id}, attempts={self.attempt_count})"

    def record_failure(self, error: str) -> None:
        """
        Record a failed purge attempt and schedule the next retry.

        Uses exponential backoff with the delays defined in RETRY_DELAYS.
        After MAX_ATTEMPTS, no more retries are scheduled (requires manual
        intervention).
        """
        from django.utils import timezone

        self.attempt_count += 1
        self.last_attempt_at = timezone.now()
        self.last_error = str(error)[:2000]  # Truncate long errors

        if self.attempt_count < self.MAX_ATTEMPTS:
            delay_seconds = self.RETRY_DELAYS[
                min(self.attempt_count - 1, len(self.RETRY_DELAYS) - 1)
            ]
            self.next_retry_at = timezone.now() + timezone.timedelta(
                seconds=delay_seconds,
            )
        else:
            # Max attempts reached - set far future date to stop retries
            self.next_retry_at = timezone.now() + timezone.timedelta(days=365)

        self.save()

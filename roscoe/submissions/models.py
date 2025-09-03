from __future__ import annotations

import contextlib
import hashlib
import logging
import uuid
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.base import File
from django.db import models
from django.db.models import Q
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from roscoe.projects.models import Project
from roscoe.submissions.constants import SubmissionFileType
from roscoe.users.models import Organization
from roscoe.users.models import User
from roscoe.workflows.models import Workflow

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

    org_part = f"org-{instance.org_id}"
    proj_part = f"proj-{instance.project.slug}" if instance.project_id else "proj-none"
    user_part = f"user-{instance.user_id}" if instance.user_id else "user-none"
    today = now()
    filename = (
        f"submissions/{org_part}/{proj_part}/{user_part}/"
        f"{today:%Y/%m/%d}/{uuid.uuid4().hex}/{filename}"
    )
    return filename


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
            # At least one of document or input_file
            models.CheckConstraint(
                name="submission_content_present",
                check=Q(document__gt="") | Q(input_file__isnull=False),
            ),
            # Not both
            models.CheckConstraint(
                name="submission_content_not_both",
                check=~(Q(document__gt="") & Q(input_file__isnull=False)),
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
    document = models.TextField(
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
        max_length=16,
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

        # Error check
        if inline_text is not None and uploaded_file is not None:
            err_msg = "Cannot provide both inline_text and uploaded_file."
            raise ValueError(err_msg)
        if inline_text is None and uploaded_file is None:
            err_msg = "Must provide either inline_text or uploaded_file."
            raise ValueError(err_msg)
        if uploaded_file is not None and not hasattr(uploaded_file, "read"):
            err_msg = "uploaded_file must be a file-like object."
            raise TypeError(err_msg)

        # Handle user content. If it's a file, we always store in FileField.
        # Otherwise, if it's text and small enough, store inline.
        # If text but too large, spill to FileField.
        if uploaded_file is not None:
            # Always rewind before using the stream
            with contextlib.suppress(Exception):
                uploaded_file.seek(0)

            # Compute checksum first
            try:
                checksum = self._compute_checksum_filelike(uploaded_file)
                with contextlib.suppress(Exception):
                    uploaded_file.seek(0)  # rewind for storage read
            except Exception:
                logger.info(
                    "Failed to compute checksum for uploaded file",
                    exc_info=True,
                )
                checksum = ""
            final_name = filename or getattr(uploaded_file, "name", "") or "upload"
            self.input_file.save(final_name, uploaded_file, save=False)
            self.original_filename = final_name
            self.size_bytes = getattr(uploaded_file, "size", 0)
            self.checksum_sha256 = checksum or self.checksum_sha256
        elif inline_text is not None:
            data = inline_text.encode("utf-8")
            self.size_bytes = len(data)
            # Provide a sane default filename for inline content (used if we spill)
            self.original_filename = filename or self.original_filename or "inline.txt"
            self.checksum_sha256 = self._compute_checksum(data)
            if len(data) <= inline_max_bytes:
                self.document = inline_text
                self.input_file = None
            else:
                # spill to file storage
                self.document = ""
                self.input_file.save(
                    self.original_filename,
                    ContentFile(data),
                    save=False,
                )
        else:
            err_msg = "No content provided."
            raise ValueError(err_msg)

        if not file_type or file_type not in SubmissionFileType.values:
            file_type = detect_file_type(
                filename=self.original_filename or filename,
                text=inline_text,
            )

        self.file_type = file_type

        # Do not self.save() here; caller will save after setting other fields
        return True

    def get_content(self) -> str:
        """
        Retrieve the actual content of this submission, whether stored
        inline or in the FileField.

        Returns:
            str: The content as a string.
        """
        if self.document:
            return self.document
        if self.input_file:
            try:
                with self.input_file.open("rb"):
                    with contextlib.suppress(Exception):
                        self.input_file.seek(0)
                    data = self.input_file.read()
            except Exception:
                return ""
            return (
                data.decode("utf-8", errors="replace")
                if isinstance(data, bytes)
                else str(data)
            )
        return ""

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

        # Content presence: require exactly one of (document, input_file)
        has_doc = bool(self.document)
        has_file = bool(self.input_file)
        if not (has_doc ^ has_file):
            errors["document"] = _("Provide exactly one of document or input_file.")

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
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to compute checksum for submission",
                    extra={"id": self.id},
                )

        # Ensure file_type is set
        if not self.file_type:
            self.file_type = detect_file_type(
                filename=self.original_filename
                or getattr(self.input_file, "name", None),
                text=self.document or None,
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


def detect_file_type(
    *,
    filename: str | None = None,
    text: str | None = None,
) -> str:
    name = (filename or "").lower()
    if name.endswith(".json"):
        return SubmissionFileType.JSON
    if name.endswith(".xml"):
        return SubmissionFileType.XML
    if name.endswith(".idf") or "energyplus" in name:
        return SubmissionFileType.ENERGYPLUS_IDF
    if text:
        s = text.lstrip()
        if s.startswith(("{", "[")):
            return SubmissionFileType.JSON
        if s.startswith("<"):
            return SubmissionFileType.XML
    return SubmissionFileType.UNKNOWN  # default fallback

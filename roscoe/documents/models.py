# roscoe/documents/models.py
from __future__ import annotations

import contextlib
import hashlib
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from roscoe.core.constants import RequestType
from roscoe.projects.models import Project
from roscoe.users.models import Organization
from roscoe.users.models import User
from roscoe.workflows.models import Workflow


def submission_upload_to(instance: Submission, filename: str) -> str:
    """
    Generate a unique upload path for submission files based on organization and project.
    """
    if not instance:
        err_msg = "Instance must be provided for upload path generation."
        raise ValueError(err_msg)
    if not isinstance(instance, Submission):
        err_msg = "Instance must be a Submission object."
        raise TypeError(err_msg)
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
    A request to validate a single file using a specific workflow version.
    """

    class Meta:
        indexes = [
            models.Index(
                fields=[
                    "org",
                    "project",
                    "workflow",
                    "created",
                ]
            ),
            models.Index(
                fields=[
                    "org",
                    "created",
                ],
            ),
        ]
        # Idempotency per org, only when client_ref is non-empty
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "org",
                    "client_ref",
                ],
                name="uniq_submission_org_client_ref_nonempty",
                condition=~Q(client_ref=""),
            )
        ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

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

    input_file = models.FileField(
        upload_to=submission_upload_to,
        help_text=_("The file to validate, e.g. IDF, JSON, XML, etc."),
    )

    original_filename = models.CharField(
        max_length=512,
        blank=True,
        default="",
    )

    content_type = models.CharField(
        max_length=128,
        blank=True,
        default="",
    )

    size_bytes = models.BigIntegerField(default=0)

    sha256 = models.CharField(
        max_length=64,
        blank=True,
        default="",
    )

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.PROTECT,
        related_name="submissions",
        help_text=_("Workflow *version* to run."),
    )

    # Optional per-run overrides (env vars, thresholds, step toggles, etc.)
    config = models.JSONField(default=dict, blank=True)

    requested_by = models.CharField(
        max_length=32,
        choices=RequestType.choices,
        blank=True,
        default="",
    )

    # Client-provided idempotency key; unique per org when provided
    client_ref = models.CharField(max_length=128, blank=True, default="")

    latest_run = models.OneToOneField(
        "validations.ValidationRun",  # keep explicit app label to avoid circular import hiccups
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # --- Validation & hygiene ------------------------------------------------
    def clean(self, *args, **kwargs):
        errors = {}

        # Require same-org relationships (DB can't enforce this natively)
        if self.project_id and self.project.org_id != self.org_id:
            errors["project"] = _("Project must belong to the same organization.")
        if self.workflow_id and self.workflow.org_id != self.org_id:
            errors["workflow"] = _("Workflow must belong to the same organization.")
        if errors:
            raise ValidationError(errors)

        # capture filename/content_type/size/sha256 if possible on first save
        if self.input_file and not self.sha256:
            self.original_filename = self.original_filename or getattr(
                self.input_file,
                "name",
                "",
            )
            with contextlib.suppress(OSError, AttributeError):
                self.size_bytes = self.input_file.size
            # Only hash small-ish uploads in request thread; for larger files,
            # do it async
            with contextlib.suppress(OSError, AttributeError, ValueError):
                hasher = hashlib.sha256()
                for chunk in self.input_file.chunks():
                    hasher.update(chunk)
                self.sha256 = hasher.hexdigest()

        super().save(*args, **kwargs)

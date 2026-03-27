"""Helpers for presenting and downloading signed validation credentials.

These helpers keep the human-facing naming rules for signed credentials in one
place. The cryptographic binding still comes from the content digest and output
hash, but people need a readable label to tell credentials apart in day-to-day
use.

The current policy is:

- prefer ``Submission.name`` as the signed human label
- otherwise fall back to the original filename, but replace ``.`` with ``_``
- otherwise use a short digest-based fallback label

The same resolved label is then used for UI display and to build a
filesystem-friendly download filename.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from django.utils.text import slugify

from validibot.core.utils import reverse_with_org

if TYPE_CHECKING:
    from django.http import HttpRequest

    from validibot.submissions.models import Submission
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)

RESOURCE_LABEL_DIGEST_PREFIX_LENGTH = 8
FILENAME_SEGMENT_MAX_LENGTH = 32
FALLBACK_IDENTIFIER_PREFIX_LENGTH = 12
DEFAULT_RESOURCE_LABEL = "Submission"
DEFAULT_RESOURCE_FILENAME_SEGMENT = "submission"
SIGNED_CREDENTIAL_FILENAME_SUFFIX = "signed-credential"


def resolve_submission_resource_label(submission: Submission) -> str:
    """Return the human-facing label that should be signed into the VC.

    The label is intentionally simple and does not inspect the submitted file
    contents. That keeps the rule format-agnostic and avoids leaking extra
    input data into the credential. When we fall back to the stored filename,
    we normalize dots to underscores so extensions do not read like sentence
    punctuation in the signed label.
    """

    explicit_name = (submission.name or "").strip()
    if explicit_name:
        return explicit_name

    original_filename = (submission.original_filename or "").strip()
    if original_filename:
        return original_filename.replace(".", "_")

    checksum = (submission.checksum_sha256 or "").strip()
    if checksum:
        digest_prefix = checksum[:RESOURCE_LABEL_DIGEST_PREFIX_LENGTH]
        return f"{DEFAULT_RESOURCE_LABEL} {digest_prefix}"

    return DEFAULT_RESOURCE_LABEL


def extract_signed_credential_resource_label(
    payload_json: Mapping[str, object] | None,
) -> str | None:
    """Extract the signed human label from a decoded VC payload."""

    if not isinstance(payload_json, Mapping):
        return None

    subject = payload_json.get("credentialSubject")
    if not isinstance(subject, Mapping):
        return None

    label = subject.get("resourceLabel")
    if not isinstance(label, str):
        return None

    label = label.strip()
    return label or None


def build_signed_credential_download_filename(
    *,
    resource_label: str | None,
    workflow_slug: str | None,
    fallback_identifier: str | None = None,
) -> str:
    """Build a filesystem-friendly filename for a downloaded credential.

    The filename is intentionally shorter and more constrained than the signed
    human label. We keep the original label in the payload for display, but use
    slugified and truncated segments for the actual download name so browsers,
    shells, and storage systems handle it predictably.
    """

    label_segment = _slug_segment(resource_label)
    if not label_segment:
        label_segment = _fallback_segment(fallback_identifier)

    workflow_segment = _slug_segment(workflow_slug)

    parts = [label_segment]
    if workflow_segment and workflow_segment != label_segment:
        parts.append(workflow_segment)
    parts.append(SIGNED_CREDENTIAL_FILENAME_SUFFIX)
    return "__".join(parts) + ".jwt"


def get_signed_credential_display_context(
    *,
    request: HttpRequest,
    run: ValidationRun,
) -> dict[str, object]:
    """Build template context for the signed-credential sidebar card.

    The Pro credential model is optional in the community app, so the
    run-detail pages need one guarded lookup path that both the standalone
    validation view and the workflow launch status view can reuse.
    """

    issued_credential = None
    credential_download_url = None
    credential_download_name = None
    credential_resource_label = None

    try:
        from validibot_pro.credentials.models import IssuedCredential

        issued_credential = IssuedCredential.objects.filter(workflow_run=run).first()
        if issued_credential:
            credential_resource_label = extract_signed_credential_resource_label(
                issued_credential.payload_json,
            )
            credential_download_name = build_signed_credential_download_filename(
                resource_label=credential_resource_label,
                workflow_slug=run.workflow.slug if run.workflow else "",
                fallback_identifier=str(run.pk),
            )
            credential_download_url = reverse_with_org(
                "validations:credential_download",
                request=request,
                kwargs={"pk": run.pk},
            )
    except Exception:
        logger.debug("Pro credential lookup unavailable", exc_info=True)

    return {
        "issued_credential": issued_credential,
        "credential_download_url": credential_download_url,
        "credential_download_name": credential_download_name,
        "credential_resource_label": credential_resource_label,
    }


def _slug_segment(value: str | None) -> str:
    """Return a safe ASCII filename segment."""

    return slugify(value or "")[:FILENAME_SEGMENT_MAX_LENGTH].strip("-")


def _fallback_segment(fallback_identifier: str | None) -> str:
    """Return a deterministic fallback segment for downloads."""

    identifier = slugify(str(fallback_identifier or ""))
    if identifier:
        identifier = identifier[:FALLBACK_IDENTIFIER_PREFIX_LENGTH]
        return f"{DEFAULT_RESOURCE_FILENAME_SEGMENT}-{identifier}"
    return DEFAULT_RESOURCE_FILENAME_SEGMENT

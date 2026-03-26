"""Helpers for presenting and downloading signed validation credentials.

These helpers keep the human-facing naming rules for signed credentials in one
place. The cryptographic binding still comes from the content digest and output
hash, but people need a readable label to tell credentials apart in day-to-day
use.

The current policy is:

- prefer ``Submission.name`` as the signed human label
- otherwise fall back to the original filename
- otherwise use a short digest-based fallback label

The same resolved label is then used for UI display and to build a
filesystem-friendly download filename.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from django.utils.text import slugify

if TYPE_CHECKING:
    from validibot.submissions.models import Submission

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
    input data into the credential.
    """

    explicit_name = (submission.name or "").strip()
    if explicit_name:
        return explicit_name

    original_filename = (submission.original_filename or "").strip()
    if original_filename:
        return original_filename

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

# simplevalidations/submissions/ingest.py
import contextlib
import hashlib
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from simplevalidations.core.filesafety import build_safe_filename
from simplevalidations.core.filesafety import detect_suspicious_magic
from simplevalidations.core.filesafety import sha256_hexdigest


@dataclass
class IngestResult:
    filename: str
    sha256: str


TEXTUAL_CT = {
    "application/json",
    "application/xml",
    "text/plain",
    "text/x-idf",
}


def prepare_inline_text(
    *,
    text: str,
    filename: str,
    content_type: str,
    deny_magic_on_text: bool = True,
) -> tuple[str, IngestResult]:
    """
    Returns (safe_filename, IngestResult). Performs magic sniff (on bytes),
    filename sanitation, and hashing. Text is treated as UTF-8 for hashing.
    """
    raw = text.encode("utf-8", errors="ignore")
    if (
        deny_magic_on_text
        and content_type in TEXTUAL_CT
        and detect_suspicious_magic(raw)
    ):
        err_msg = _("Binary/archived file not allowed for this content type.")
        raise ValidationError(err_msg)

    safe_name = build_safe_filename(
        filename,
        content_type=content_type,
        fallback="document",
    )
    digest = sha256_hexdigest(raw)
    return safe_name, IngestResult(filename=safe_name, sha256=digest)


def prepare_uploaded_file(
    *,
    uploaded_file,
    filename: str,
    content_type: str,
    max_bytes: int,
) -> IngestResult:
    """
    Stream through uploaded_file to compute SHA-256 and enforce size.
    Returns IngestResult. Caller should pass the same uploaded_file to storage.
    """
    # safety: use provided filename or file.name
    effective_name = filename or getattr(uploaded_file, "name", "") or "document"
    safe_name = build_safe_filename(effective_name, content_type=content_type)

    # stream to avoid memory spikes
    total = 0
    h = hashlib.sha256()
    first = None
    for chunk in uploaded_file.chunks():
        if first is None:
            first = chunk
        total += len(chunk)
        if total > max_bytes:
            raise ValidationError(_("File too large."))
        h.update(chunk)
    if first and content_type in TEXTUAL_CT and detect_suspicious_magic(first):
        raise ValidationError(
            _("Binary/archived file not allowed for this content type."),
        )
    digest = h.hexdigest()
    with contextlib.suppress(AttributeError, OSError):
        uploaded_file.seek(0)
    return IngestResult(filename=safe_name, sha256=digest)

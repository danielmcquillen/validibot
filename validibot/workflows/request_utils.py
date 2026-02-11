import json
import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from validibot.workflows.constants import SUPPORTED_CONTENT_TYPES

logger = logging.getLogger(__name__)


class SubmissionRequestMode(StrEnum):
    RAW_BODY = "raw_body"
    JSON_ENVELOPE = "json_envelope"
    MULTIPART = "multipart"
    UNKNOWN = "unknown"


class ModeDetectionResult(BaseModel):
    """
    Result of detecting the submission request mode
    when an incoming API request is first received.
    """

    mode: SubmissionRequestMode
    content_type: str
    parsed_envelope: dict[str, Any] | None = None
    error: str | None = None

    model_config = {"arbitrary_types_allowed": True}

    @property
    def has_error(self) -> bool:
        return bool(self.error)


# ---------------------- Mode Detection Helpers ----------------------


def extract_request_basics(request) -> tuple[str, bytes]:
    """
    Normalize frequently accessed request attributes.
    """
    content_type_header = (request.content_type or "").split(";")[0].lower()
    body_bytes = request.body if request.body else b""
    return content_type_header, body_bytes


def detect_mode(
    request,
    content_type_header: str,
    body_bytes: bytes,
) -> ModeDetectionResult:
    """
    Decide which submission mode applies to an incoming request.
    """
    if content_type_header.startswith("multipart/"):
        return ModeDetectionResult(
            mode=SubmissionRequestMode.MULTIPART,
            content_type=content_type_header,
        )

    if content_type_header == "application/json":
        parsed = None
        if body_bytes:
            try:
                parsed = json.loads(body_bytes.decode("utf-8"))
            except Exception as exc:
                message = f"Invalid JSON payload: {exc}"
                logger.warning(
                    "JSON envelope detection failed: %s",
                    exc,
                    exc_info=True,
                )
                return ModeDetectionResult(
                    mode=SubmissionRequestMode.UNKNOWN,
                    content_type=content_type_header,
                    error=message,
                )
        if isinstance(parsed, dict) and "content" in parsed:
            return ModeDetectionResult(
                mode=SubmissionRequestMode.JSON_ENVELOPE,
                content_type=content_type_header,
                parsed_envelope=parsed,
            )
        if content_type_header in SUPPORTED_CONTENT_TYPES:
            return ModeDetectionResult(
                mode=SubmissionRequestMode.RAW_BODY,
                content_type=content_type_header,
            )

    if content_type_header in SUPPORTED_CONTENT_TYPES:
        return ModeDetectionResult(
            mode=SubmissionRequestMode.RAW_BODY,
            content_type=content_type_header,
        )

    if not content_type_header:
        return ModeDetectionResult(
            mode=SubmissionRequestMode.UNKNOWN,
            content_type="",
            error="Missing Content-Type header.",
        )

    return ModeDetectionResult(
        mode=SubmissionRequestMode.UNKNOWN,
        content_type=content_type_header,
        error=f"Unsupported Content-Type '{content_type_header}'.",
    )

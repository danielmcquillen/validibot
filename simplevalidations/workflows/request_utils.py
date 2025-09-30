import json
import logging

from simplevalidations.workflows.constants import SUPPORTED_CONTENT_TYPES

logger = logging.getLogger(__name__)


# ---------------------- Mode Detection Helpers ----------------------


def extract_request_basics(request) -> tuple[str, bytes]:
    """
    Normalize frequently accessed request attributes.
    """
    content_type_header = (request.content_type or "").split(";")[0].lower()
    body_bytes = request.body if request.body else b""
    return content_type_header, body_bytes


def is_raw_body_mode(
    request,
    content_type_header: str,
    body_bytes: bytes,
) -> bool:
    """
    Decide if this request should be treated as Mode 1 (raw body).
    Criteria:
        - NOT multipart/*
        - Content-Type is in SUPPORTED_CONTENT_TYPES
        - If application/json and JSON parses into object containing 'content',
        treat as envelope instead (return False).
    """
    if content_type_header.startswith("multipart/"):
        return False
    if content_type_header not in SUPPORTED_CONTENT_TYPES:
        return False
    if content_type_header == "application/json":
        # quick cheap check to avoid decode when obviously not JSON
        if body_bytes[:1] == b"{":
            try:
                probe = json.loads(body_bytes.decode("utf-8"))
                if isinstance(probe, dict) and "content" in probe:
                    return False
            except Exception:
                logger.debug(
                    "JSON raw probe failed (still treating as raw).",
                    exc_info=True,
                )
    return True

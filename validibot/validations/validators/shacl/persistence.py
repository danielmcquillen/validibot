"""SHACL persistence helpers: turn form data into Ruleset content.

Shared by both the workflow step builder
(``validibot.workflows.views_helpers.build_shacl_config``) and the
library-validator service
(``validibot.validations.utils.create_shacl_library_validator``) so the
file-concatenation + per-file metadata format stays consistent across
both paths. The engine reads ``Ruleset.rules`` as one Turtle blob — the
boundary comments below are valid Turtle comments (line-prefixed with
``#``) that rdflib silently ignores at parse time, while still being
visible to anyone inspecting the merged text in admin.
"""

from __future__ import annotations

import hashlib
from typing import Any


def read_uploaded_text(uploaded: Any) -> tuple[str, int, str]:
    """Read an uploaded file as UTF-8 text + return (text, size_bytes, sha256).

    Used by both the step builder and the library validator service for
    shape/ontology uploads. The sha256 ends up in ``Ruleset.metadata``
    so signed attestations can pin the exact bytes that drove the
    validation.

    Caller is responsible for ensuring the uploaded file has not been
    consumed earlier in the request lifecycle (we ``seek(0)`` defensively
    but can't recover from a stream that's already been drained).
    """
    uploaded.seek(0)
    raw_bytes = uploaded.read()
    uploaded.seek(0)
    size = len(raw_bytes)
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    text = (
        raw_bytes.decode("utf-8", errors="replace")
        if isinstance(raw_bytes, bytes)
        else str(raw_bytes or "")
    )
    return text, size, sha256


def concatenate_uploaded_files(
    uploaded_files: list[Any] | None,
    inline_text: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Concatenate uploaded files + inline text with file-boundary comments.

    Produces ``(combined_text, file_metadata_list)``. Each file gets a
    Turtle comment header
    (``# === File: name.ttl (sha256: <12-char-prefix>...) ===``) so
    that an operator viewing the merged ``rules_text`` in admin can see
    where each upload begins.

    The engine treats the concatenated text as one Turtle blob; the
    boundary comments are ignored at parse time. The metadata list is
    what the UI uses to render the read-only "currently attached"
    panel on the step / library validator detail pages.
    """
    parts: list[str] = []
    files_meta: list[dict[str, Any]] = []
    for uploaded in uploaded_files or []:
        name = getattr(uploaded, "name", "unnamed")
        text, size, sha256 = read_uploaded_text(uploaded)
        files_meta.append({"name": name, "size_bytes": size, "sha256": sha256})
        parts.append(f"# === File: {name} (sha256: {sha256[:12]}...) ===\n{text}")
    if inline_text:
        parts.append(f"# === File: <inline> ===\n{inline_text}")
    return "\n\n".join(parts), files_meta

# roscoe/filesafety.py
import hashlib
import os
import re
import unicodedata
from typing import Optional

SAFE_EXT_FOR_TYPE = {
    # keep this aligned with SUPPORTED_CONTENT_TYPES & SubmissionFileType
    "application/json": ".json",
    "application/xml": ".xml",
    "text/plain": ".txt",
    "text/x-idf": ".idf",
    # "application/yaml": ".yaml",   # uncomment when you allow YAML
    # "text/yaml":        ".yaml",
}

SUSPICIOUS_MAGIC_PREFIXES = (
    b"PK\x03\x04",  # zip/jar/docx/xlsx
    b"%PDF",  # pdf
    b"\x7fELF",  # elf
    b"MZ",  # windows pe
)

# allow common filename chars; collapse whitespace; drop control chars
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._\-()+=,@ ]+")


def sanitize_filename(candidate: str, *, fallback: str = "document") -> str:
    """
    Ensure basename only, normalize unicode, strip dangerous chars, trim length.
    """
    candidate = candidate or fallback
    # basename & normalize unicode
    name = os.path.basename(candidate)
    name = unicodedata.normalize("NFKC", name)

    # strip control chars
    name = "".join(ch for ch in name if 32 <= ord(ch) < 127 or ch in "\t")
    name = _FILENAME_SAFE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        name = fallback

    # avoid dotfiles & trailing dots
    if name.startswith("."):
        name = name.lstrip(".") or fallback
    name = name.rstrip(".")

    # cap length (leave room for extension changes)
    return name[:100]


def force_extension(
    name: str,
    *,
    content_type: str,
    default_ext: Optional[str] = None,
) -> str:
    want_ext = SAFE_EXT_FOR_TYPE.get(content_type, default_ext or ".txt")
    root, ext = os.path.splitext(name)
    # if ext mismatches, replace it
    if ext.lower() != want_ext.lower():
        name = f"{root}{want_ext}"
    return name


def build_safe_filename(
    original: str,
    *,
    content_type: str,
    fallback: str = "document",
) -> str:
    name = sanitize_filename(original, fallback=fallback)
    return force_extension(name, content_type=content_type)


def detect_suspicious_magic(raw: bytes) -> bool:
    head = raw[:4]
    return any(
        head.startswith(prefix[: len(head)]) for prefix in SUSPICIOUS_MAGIC_PREFIXES
    )


def sha256_hexdigest(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()

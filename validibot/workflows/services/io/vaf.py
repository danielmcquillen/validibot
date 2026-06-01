"""The Validibot Archive Format (``.vaf``) — package and read a workflow bundle.

A ``.vaf`` file is an ordinary ZIP archive holding, at minimum:

- ``manifest.json`` — format/version metadata about the archive itself.
- ``workflow.json`` — the workflow *definition* (its own ``format_version``).
- ``files/<sha256>`` — optional binary blobs (step-owned resource uploads)
  referenced from ``workflow.json`` by their content hash.

Why an archive and not bare JSON: a definition can reference uploaded files
(FMU models, weather files, templates) that JSON can't carry inline. Packaging
them by content hash keeps ``workflow.json`` small and lets the importer restore
the exact bytes. A *bare* ``workflow.json`` is still accepted on import for the
common file-free case (e.g. the Darwin Core example) — but only then; a JSON
whose definition needs bundled files is rejected, because the bytes aren't
there. That is the deliberate ".json works until we require files" rule.

Everything here is pure (no Django, no models) so it stays unit-testable and has
a single responsibility: turning ``(definition dict, {hash: bytes})`` into ZIP
bytes and back. Reconstructing the workflow graph is the importer's job.

Security: reads are defensive. The archive is parsed in memory with a total-size
cap and a per-member cap, only the known members are read (``manifest.json``,
``workflow.json``, ``files/<hex>``), and any entry with an absolute path,
``..`` traversal, or an unexpected name is rejected — a ``.vaf`` is untrusted
user input.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from dataclasses import field
from hashlib import sha256
from typing import Any

# ── Archive layout ──────────────────────────────────────────────────────
VAF_VERSION = 1
MANIFEST_NAME = "manifest.json"
WORKFLOW_JSON_NAME = "workflow.json"
FILES_PREFIX = "files/"
ARCHIVE_EXTENSION = ".vaf"
JSON_EXTENSION = ".json"

# ── Safety caps. Generous for real workflows, hostile to zip bombs. ─────
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024  # 50 MB compressed input
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB total inflated
MAX_MEMBER_COUNT = 5000
# A content hash member name is exactly ``files/`` + 64 lowercase hex chars.
_FILE_MEMBER_RE = re.compile(r"^files/[0-9a-f]{64}$")


class VafError(Exception):
    """A malformed or unsafe archive/definition input.

    Carries a machine-readable ``code`` (``vaf.*``) alongside the human message
    so the import view can render a precise reason on the error page without
    string-matching.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VafBundle:
    """The parsed contents of a ``.vaf`` (or bare ``.json``) input.

    ``workflow`` is the parsed ``workflow.json`` definition dict. ``files`` maps
    a content hash to its raw bytes (empty for a bare-JSON import). ``manifest``
    is the parsed ``manifest.json`` (synthesised for bare JSON). ``had_archive``
    records whether the input was a real archive, so the importer can give a
    precise error if a bare JSON turns out to need bundled files.
    """

    workflow: dict[str, Any]
    files: dict[str, bytes] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    had_archive: bool = True


def content_hash(data: bytes) -> str:
    """Return the lowercase hex SHA-256 used as a bundled file's key/name."""
    return sha256(data).hexdigest()


def pack(
    workflow: dict[str, Any],
    *,
    files: dict[str, bytes] | None = None,
    manifest_extra: dict[str, Any] | None = None,
) -> bytes:
    """Pack a definition (and any referenced files) into ``.vaf`` ZIP bytes.

    ``files`` maps content hash -> bytes; each is stored at ``files/<hash>``.
    ``manifest_extra`` lets the exporter stamp provenance (exported_at, by whom,
    app version) without this module reaching for a clock it shouldn't own.
    """
    files = files or {}
    manifest = {
        "vaf_version": VAF_VERSION,
        "kind": "workflow",
        "contents": [MANIFEST_NAME, WORKFLOW_JSON_NAME]
        + [f"{FILES_PREFIX}{h}" for h in sorted(files)],
        **(manifest_extra or {}),
    }

    buffer = io.BytesIO()
    # Deterministic member order (manifest, workflow, then sorted files) so the
    # same input yields byte-stable archives — important for the committed
    # example artifacts and for round-trip tests.
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, _dump(manifest))
        archive.writestr(WORKFLOW_JSON_NAME, _dump(workflow))
        for file_hash in sorted(files):
            archive.writestr(f"{FILES_PREFIX}{file_hash}", files[file_hash])
    return buffer.getvalue()


def read_input(data: bytes, *, filename: str | None = None) -> VafBundle:
    """Parse uploaded bytes as a ``.vaf`` archive or a bare ``workflow.json``.

    Dispatch is by content, not just extension: a ZIP magic number means treat
    it as an archive; otherwise it is parsed as bare JSON. ``filename`` only
    sharpens error messages. Either way the result is a :class:`VafBundle`.
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        msg = (
            f"File is larger than the {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB "
            f"import limit."
        )
        raise VafError(msg, code="vaf.too_large")
    if not data:
        raise VafError("The uploaded file is empty.", code="vaf.empty")

    if _looks_like_zip(data):
        return _read_archive(data)
    return _read_bare_json(data, filename=filename)


# ───────────────────────────────────────────────────────── internals ──


def _dump(value: dict[str, Any]) -> bytes:
    """Serialise a dict to stable, human-diffable JSON bytes."""
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode(
        "utf-8",
    )


def _looks_like_zip(data: bytes) -> bool:
    """True when the bytes start with a ZIP local-file-header magic number."""
    # ``PK\x03\x04`` (normal) or ``PK\x05\x06`` (empty archive).
    return data[:2] == b"PK"


def _read_bare_json(data: bytes, *, filename: str | None) -> VafBundle:
    """Parse bare ``workflow.json`` bytes (no archive, hence no files)."""
    try:
        workflow = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        label = filename or "file"
        msg = f"{label} is not a valid .vaf archive or workflow.json: {exc}"
        raise VafError(msg, code="vaf.invalid_json") from exc
    if not isinstance(workflow, dict):
        raise VafError(
            "workflow.json must be a JSON object.",
            code="vaf.invalid_json",
        )
    return VafBundle(
        workflow=workflow,
        files={},
        manifest={"vaf_version": VAF_VERSION, "kind": "workflow", "synthesised": True},
        had_archive=False,
    )


def _read_archive(data: bytes) -> VafBundle:
    """Parse ``.vaf`` ZIP bytes into a bundle, reading only known members."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise VafError(
            "The file is not a readable .vaf archive.", code="vaf.bad_zip"
        ) from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBER_COUNT:
            raise VafError(
                "The archive contains too many entries.",
                code="vaf.too_many_members",
            )
        total = sum(info.file_size for info in infos)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise VafError(
                "The archive inflates to more than the allowed size.",
                code="vaf.too_large_inflated",
            )

        names = {info.filename for info in infos}
        if WORKFLOW_JSON_NAME not in names:
            raise VafError(
                "The archive does not contain a workflow.json.",
                code="vaf.missing_workflow",
            )

        workflow = _read_member_json(archive, WORKFLOW_JSON_NAME)
        manifest = (
            _read_member_json(archive, MANIFEST_NAME)
            if MANIFEST_NAME in names
            else {"vaf_version": VAF_VERSION, "kind": "workflow"}
        )
        files = _read_member_files(archive, infos)

    _validate_manifest(manifest)
    return VafBundle(
        workflow=workflow,
        files=files,
        manifest=manifest,
        had_archive=True,
    )


def _read_member_json(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    """Read and parse a single JSON member, guarding decode/parse failures."""
    try:
        raw = archive.read(name)
    except KeyError as exc:
        raise VafError(
            f"Archive member {name!r} is missing.", code="vaf.missing_member"
        ) from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VafError(
            f"Archive member {name!r} is not valid JSON.", code="vaf.invalid_json"
        ) from exc
    if not isinstance(value, dict):
        raise VafError(
            f"Archive member {name!r} must be a JSON object.", code="vaf.invalid_json"
        )
    return value


def _read_member_files(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
) -> dict[str, bytes]:
    """Read ``files/<sha256>`` members, verifying each name and its hash.

    Any entry outside the known names is ignored unless it *looks* like a files
    entry with a bad/traversing name, which is rejected outright — we never read
    an attacker-named path, and we verify the stored bytes actually hash to the
    name they claim (so a tampered archive can't smuggle mismatched content).
    """
    files: dict[str, bytes] = {}
    for info in infos:
        name = info.filename
        if name in (MANIFEST_NAME, WORKFLOW_JSON_NAME):
            continue
        if not name.startswith(FILES_PREFIX):
            # Unknown sibling member — ignore (forward-compatible), but never
            # allow a path-traversal name to slip through as "unknown".
            if name.startswith("/") or ".." in name.split("/"):
                raise VafError(
                    f"Archive contains an unsafe path: {name!r}.",
                    code="vaf.unsafe_path",
                )
            continue
        if not _FILE_MEMBER_RE.match(name):
            raise VafError(
                f"Archive file entry has an unexpected name: {name!r}.",
                code="vaf.unsafe_path",
            )
        declared_hash = name[len(FILES_PREFIX) :]
        payload = archive.read(name)
        if content_hash(payload) != declared_hash:
            raise VafError(
                f"Bundled file {declared_hash!r} does not match its content hash.",
                code="vaf.hash_mismatch",
            )
        files[declared_hash] = payload
    return files


def _validate_manifest(manifest: dict[str, Any]) -> None:
    """Reject archives whose manifest declares an unsupported format version."""
    version = manifest.get("vaf_version")
    if version is not None and version != VAF_VERSION:
        raise VafError(
            f"Unsupported .vaf version {version!r}; this server supports "
            f"version {VAF_VERSION}.",
            code="vaf.unsupported_version",
        )

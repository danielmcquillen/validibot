"""
THMX/THMZ file parser.

Parses THERM XML files into a ThermModel dataclass for domain checks
and signal extraction. Handles both .thmx (raw XML) and .thmz (ZIP
archive containing XML) formats.

TODO: The XML element names and attribute names used here are
placeholders. The actual THERM XML schema should be determined by
examining real THERM files and LBNL documentation, not by copying
from existing open-source tooling.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

from validibot.validations.validators.therm.models import ThermModel

logger = logging.getLogger(__name__)

# Maximum uncompressed size we will extract from a single member of a .thmz
# ZIP archive. THMZ archives are opened and read entirely in-process inside
# the Django/Celery THERM validator, so a small "zip bomb" with a very high
# compression ratio could otherwise decompress to many gigabytes and OOM the
# worker. 50 MiB is comfortably above any legitimate THERM model XML while
# bounding the memory a malicious archive can force us to allocate.
MAX_THMZ_UNCOMPRESSED_BYTES = 50 * 1024 * 1024


def parse_therm_file(
    content: str | bytes,
    filename: str | None = None,
) -> ThermModel:
    """
    Parse a THERM file (THMX or THMZ) into a ThermModel.

    For .thmz files (detected by filename or by attempting ZIP extraction),
    the archive is unpacked and the primary model XML is identified and
    parsed.

    For .thmx files, the content is parsed directly as XML.

    Args:
        content: Raw file content (str or bytes).
        filename: Original filename, used to detect format.

    Raises:
        ValueError: If the file cannot be parsed as THERM.
    """
    try:
        from lxml import etree
    except ImportError as exc:
        msg = "lxml is required for THERM validation but is not installed."
        raise ImportError(msg) from exc

    raw_bytes = content.encode("utf-8") if isinstance(content, str) else content

    # Detect format: THMZ (ZIP archive) or THMX (raw XML)
    is_thmz = (filename and filename.lower().endswith(".thmz")) or _is_zip(raw_bytes)

    if is_thmz:
        xml_bytes = _extract_xml_from_thmz(raw_bytes)
        source_format = "thmz"
    else:
        xml_bytes = raw_bytes
        source_format = "thmx"

    # Parse XML
    try:
        parser = etree.XMLParser(
            recover=False,
            remove_blank_text=True,
            resolve_entities=False,
            no_network=True,
        )
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        msg = f"Invalid XML in THERM file: {exc}"
        raise ValueError(msg) from exc

    return _build_model(root, source_format=source_format)


def _is_zip(data: bytes) -> bool:
    """Check if data starts with ZIP magic bytes."""
    return data[:4] == b"PK\x03\x04"


def _read_member_bounded(zf: zipfile.ZipFile, name: str) -> bytes:
    """
    Read a single ZIP member, refusing anything larger than the cap.

    THMZ archives are decompressed entirely in-process, so an attacker can
    supply a tiny archive whose members declare (or actually decompress to)
    many gigabytes — a classic "zip bomb" — and exhaust the worker's memory.
    We defend in two layers:

    1. Reject up front if the member's declared uncompressed size
       (``ZipInfo.file_size``) already exceeds ``MAX_THMZ_UNCOMPRESSED_BYTES``.
       This catches honest-but-huge archives cheaply, without decompressing.
    2. Stream the member and stop as soon as the decompressed bytes exceed the
       cap. This catches archives that *lie* about ``file_size`` (the value is
       attacker-controlled metadata), so we never materialise more than roughly
       the cap in memory regardless of what the header claims.

    Args:
        zf: The open ZIP archive to read from.
        name: The member name to extract.

    Returns:
        The decompressed bytes of the member.

    Raises:
        ValueError: If the member exceeds ``MAX_THMZ_UNCOMPRESSED_BYTES`` by
            either its declared size or its actual decompressed length.
    """
    info = zf.getinfo(name)
    if info.file_size > MAX_THMZ_UNCOMPRESSED_BYTES:
        msg = (
            "THMZ archive member "
            f"{name!r} declares an uncompressed size of {info.file_size} bytes, "
            f"which exceeds the {MAX_THMZ_UNCOMPRESSED_BYTES}-byte limit."
        )
        raise ValueError(msg)

    # Read one byte past the cap so we can detect a member that under-reports
    # its size in the header and actually decompresses to something larger.
    with zf.open(name) as member:
        data = member.read(MAX_THMZ_UNCOMPRESSED_BYTES + 1)
    if len(data) > MAX_THMZ_UNCOMPRESSED_BYTES:
        msg = (
            "THMZ archive member "
            f"{name!r} decompresses to more than the "
            f"{MAX_THMZ_UNCOMPRESSED_BYTES}-byte limit."
        )
        raise ValueError(msg)
    return data


def _extract_xml_from_thmz(data: bytes) -> bytes:
    """
    Extract the primary model XML from a THMZ ZIP archive.

    THMZ archives contain multiple files. The primary model
    file is identified by extension (.thmx) or as the largest
    XML file in the archive.

    Every member is read through :func:`_read_member_bounded`, which enforces
    ``MAX_THMZ_UNCOMPRESSED_BYTES`` so a high-compression-ratio archive cannot
    OOM the in-process validator.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Look for a .thmx file first
            thmx_files = [n for n in zf.namelist() if n.lower().endswith(".thmx")]
            if thmx_files:
                return _read_member_bounded(zf, thmx_files[0])

            # Fall back to any XML file
            xml_files = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if xml_files:
                largest = max(xml_files, key=lambda n: zf.getinfo(n).file_size)
                return _read_member_bounded(zf, largest)

            msg = "THMZ archive does not contain a .thmx or .xml file."
            raise ValueError(msg)
    except zipfile.BadZipFile as exc:
        msg = f"Invalid THMZ archive: {exc}"
        raise ValueError(msg) from exc


def _build_model(root: Any, source_format: str) -> ThermModel:
    """
    Build a ThermModel from a parsed XML root element.

    TODO: Implement XML element traversal based on the actual
    THERM XML schema. Element names, attribute names, and
    structure should be determined from real THERM files and
    LBNL documentation.
    """
    model = ThermModel(
        source_format=source_format,
        therm_version=None,
    )
    model.xml_root = root

    # TODO: parse materials, polygons, boundary conditions,
    # mesh parameters, U-factor tags, etc. from the XML tree

    return model

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

from validibot.validations.engines.therm.models import ThermModel

logger = logging.getLogger(__name__)


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
        parser = etree.XMLParser(recover=False, remove_blank_text=True)
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        msg = f"Invalid XML in THERM file: {exc}"
        raise ValueError(msg) from exc

    return _build_model(root, source_format=source_format)


def _is_zip(data: bytes) -> bool:
    """Check if data starts with ZIP magic bytes."""
    return data[:4] == b"PK\x03\x04"


def _extract_xml_from_thmz(data: bytes) -> bytes:
    """
    Extract the primary model XML from a THMZ ZIP archive.

    THMZ archives contain multiple files. The primary model
    file is identified by extension (.thmx) or as the largest
    XML file in the archive.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Look for a .thmx file first
            thmx_files = [n for n in zf.namelist() if n.lower().endswith(".thmx")]
            if thmx_files:
                return zf.read(thmx_files[0])

            # Fall back to any XML file
            xml_files = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if xml_files:
                largest = max(xml_files, key=lambda n: zf.getinfo(n).file_size)
                return zf.read(largest)

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

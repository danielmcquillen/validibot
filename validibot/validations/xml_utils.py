"""
Secure XML-to-dict conversion for CEL assertion evaluation.

Converts XML documents into nested Python dicts so that CEL expressions
can evaluate against XML data using familiar dot/bracket paths. This is
the standard approach used by policy engines (including Google's CEL usage
in GCP policy contexts): the XML never hits the CEL evaluator directly —
a parser converts it to a nested dict first.

Uses ``defusedxml`` for safe parsing, which blocks XXE attacks, entity
expansion bombs, and external entity resolution out of the box.

Conversion rules (standard xmltodict-style):
  - Element with text only → ``{"tag": "text"}``
  - Element with children → ``{"tag": {"child1": ..., "child2": ...}}``
  - Repeated sibling elements → ``{"tag": [item1, item2, ...]}``
  - Attributes → ``{"@attr": "val", ...}`` within the element dict
  - Mixed text + children → text stored in ``"#text"`` key

Example::

    >>> xml_to_dict('<root><item>hello</item><count>3</count></root>')
    {'root': {'item': 'hello', 'count': '3'}}

    >>> xml_to_dict('<root><item>a</item><item>b</item></root>')
    {'root': {'item': ['a', 'b']}}
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from defusedxml import ElementTree as SafeET

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element
from defusedxml.common import DTDForbidden
from defusedxml.common import EntitiesForbidden
from defusedxml.common import ExternalReferenceForbidden

logger = logging.getLogger(__name__)

# Default maximum XML payload size (10 MB).
DEFAULT_MAX_SIZE_BYTES = 10_000_000


class XmlParseError(ValueError):
    """Raised when XML content cannot be parsed or converted to a dict."""


def xml_to_dict(
    xml_content: str | bytes,
    *,
    strip_namespaces: bool = True,
    max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
) -> dict[str, Any]:
    """
    Parse XML content and convert it to a nested Python dict.

    This is the primary entry point for XML-to-dict conversion. The
    resulting dict can be passed directly to ``_build_cel_context()``
    or ``_resolve_path()`` for CEL assertion evaluation.

    Args:
        xml_content: Raw XML as a string or bytes.
        strip_namespaces: If True (default), strip namespace prefixes
            from element and attribute names (e.g.,
            ``{http://...}element`` → ``element``).
        max_size_bytes: Maximum allowed payload size in bytes.
            Defaults to 10 MB.

    Returns:
        A nested dict representing the XML document.

    Raises:
        XmlParseError: If the content is empty, too large, or
            cannot be parsed as valid XML.
    """
    if not xml_content:
        raise XmlParseError("Empty XML content.")

    raw_bytes = (
        xml_content.encode("utf-8") if isinstance(xml_content, str) else xml_content
    )
    if len(raw_bytes) > max_size_bytes:
        raise XmlParseError(
            f"XML payload exceeds maximum size "
            f"({len(raw_bytes):,} bytes > {max_size_bytes:,} bytes)."
        )

    try:
        root = SafeET.fromstring(raw_bytes, forbid_dtd=True)
    except (EntitiesForbidden, ExternalReferenceForbidden, DTDForbidden) as exc:
        logger.warning("Blocked dangerous XML content: %s", exc)
        raise XmlParseError(
            "XML contains forbidden constructs "
            "(entities, external references, or DTD declarations)."
        ) from exc
    except SafeET.ParseError as exc:
        logger.warning("Failed to parse XML content: %s", exc)
        raise XmlParseError(f"Invalid XML: {exc}") from exc

    tag = _strip_ns(root.tag) if strip_namespaces else root.tag
    result = {tag: _element_to_dict(root, strip_namespaces=strip_namespaces)}

    element_count = _count_elements(root)
    logger.debug(
        "XML-to-dict conversion complete: %d element(s), root tag=%r",
        element_count,
        tag,
    )
    return result


def _strip_ns(tag: str) -> str:
    """Strip the namespace URI from an element tag or attribute name."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _element_to_dict(
    element: Element,
    *,
    strip_namespaces: bool,
) -> Any:
    """
    Recursively convert an XML element to a dict, string, or list.

    Returns a plain string when the element has no attributes and no
    children (leaf text node). Otherwise returns a dict with child
    elements, attributes (prefixed with ``@``), and optional ``#text``.
    """
    result: dict[str, Any] = {}

    # Attributes (prefixed with @)
    for attr_name, attr_value in element.attrib.items():
        key = f"@{_strip_ns(attr_name)}" if strip_namespaces else f"@{attr_name}"
        result[key] = attr_value

    # Child elements
    for child in element:
        child_tag = _strip_ns(child.tag) if strip_namespaces else child.tag
        child_value = _element_to_dict(child, strip_namespaces=strip_namespaces)

        if child_tag in result:
            # Convert to list on second occurrence
            existing = result[child_tag]
            if isinstance(existing, list):
                existing.append(child_value)
            else:
                result[child_tag] = [existing, child_value]
        else:
            result[child_tag] = child_value

    # Text content
    text = (element.text or "").strip()
    if text:
        if result:
            # Has children or attributes — store text under #text
            result["#text"] = text
        else:
            # Leaf node — return plain string
            return text

    # If no attributes, no children, and no text → empty string
    if not result:
        return ""

    return result


def _count_elements(element: Element) -> int:
    """Count total elements in the tree (including root)."""
    return 1 + sum(_count_elements(child) for child in element)

"""
THMX/THMZ file parser.

Parses THERM XML files into a ThermModel dataclass for domain checks
and signal extraction. Handles both .thmx (raw XML) and .thmz (ZIP
archive containing XML) formats.

THMX XML structure (based on LBNL THERM 7.x/8.x):

    <THERM-XML xmlns="http://windows.lbl.gov">
      <ThermVersion>...</ThermVersion>
      <MeshControl MeshLevel="8" ErrorCheckFlag="1" ... />
      <Materials>
        <Material Name="..." Type="..." Conductivity="..." Emissivity="..." ... />
      </Materials>
      <BoundaryConditions>
        <BoundaryCondition Name="..." Type="..." H="..." Temperature="..." ... />
      </BoundaryConditions>
      <Polygons>
        <Polygon ID="..." Material="..." NSides="..." ...>
          <Point index="0" x="..." y="..." />
        </Polygon>
      </Polygons>
      <Boundaries>
        <BCPolygon ID="..." BC="..." UFactorTag="..." PolygonID="..." ...>
          <Point index="0" x="..." y="..." />
        </BCPolygon>
      </Boundaries>
    </THERM-XML>
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Any

from validibot.validations.engines.therm.models import ThermBoundaryCondition
from validibot.validations.engines.therm.models import ThermMaterial
from validibot.validations.engines.therm.models import ThermMeshParameters
from validibot.validations.engines.therm.models import ThermModel
from validibot.validations.engines.therm.models import ThermPolygon
from validibot.validations.engines.therm.models import ThermUFactorTag

logger = logging.getLogger(__name__)

# THERM XML namespace
THERM_NS = "http://windows.lbl.gov"
NS_MAP = {"t": THERM_NS}


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

    THMZ archives typically contain multiple files. The primary model
    file is the one with a .thmx extension, or failing that, the largest
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
                # Pick the largest XML file as the primary model
                largest = max(xml_files, key=lambda n: zf.getinfo(n).file_size)
                return zf.read(largest)

            msg = "THMZ archive does not contain a .thmx or .xml file."
            raise ValueError(msg)
    except zipfile.BadZipFile as exc:
        msg = f"Invalid THMZ archive: {exc}"
        raise ValueError(msg) from exc


def _build_model(root: Any, source_format: str) -> ThermModel:
    """Build a ThermModel from a parsed XML root element."""
    ns = _detect_namespace(root)

    model = ThermModel(
        source_format=source_format,
        therm_version=_get_text(root, "ThermVersion", ns),
    )

    # Mesh parameters
    mesh_el = _find(root, "MeshControl", ns)
    if mesh_el is not None:
        model.mesh_params = ThermMeshParameters(
            mesh_level=_attr_int(mesh_el, "MeshLevel"),
            error_limit=_attr_float(mesh_el, "ErrorLimit"),
        )

    # Materials
    materials_el = _find(root, "Materials", ns)
    if materials_el is not None:
        for mat_el in _findall(materials_el, "Material", ns):
            mat = _parse_material(mat_el)
            if mat:
                model.materials[mat.id] = mat

    # Boundary conditions
    bcs_el = _find(root, "BoundaryConditions", ns)
    if bcs_el is not None:
        for bc_el in _findall(bcs_el, "BoundaryCondition", ns):
            bc = _parse_boundary_condition(bc_el)
            if bc:
                model.boundary_conditions[bc.id] = bc

    # Polygons
    polys_el = _find(root, "Polygons", ns)
    if polys_el is not None:
        for poly_el in _findall(polys_el, "Polygon", ns):
            poly = _parse_polygon(poly_el, ns)
            if poly:
                model.polygons.append(poly)

    # Boundaries (BCPolygon elements) - extract U-factor tags
    bounds_el = _find(root, "Boundaries", ns)
    if bounds_el is not None:
        seen_tags: set[str] = set()
        for bc_poly_el in _findall(bounds_el, "BCPolygon", ns):
            tag_name = bc_poly_el.get("UFactorTag", "").strip()
            if tag_name and tag_name not in seen_tags:
                seen_tags.add(tag_name)
                model.ufactor_tags.append(
                    ThermUFactorTag(name=tag_name, tag_type="boundary"),
                )

    # CMA and glazing system flags
    cma_el = _find(root, "CMABestWorstOptions", ns)
    model.has_cma_data = cma_el is not None

    glazing_el = _find(root, "GlazingSystemData", ns)
    if glazing_el is None:
        glazing_el = _find(root, "GlazingSystems", ns)
    model.has_glazing_system = glazing_el is not None

    model.xml_root = root
    return model


def _parse_material(el: Any) -> ThermMaterial | None:
    """Parse a <Material> element into a ThermMaterial."""
    name = el.get("Name", "").strip()
    if not name:
        return None
    return ThermMaterial(
        id=name,  # THERM uses material Name as identifier
        name=name,
        material_type=el.get("Type", "").strip(),
        conductivity=_attr_float(el, "Conductivity"),
        emissivity_inside=_attr_float(el, "EmissivityBack"),
        emissivity_outside=_attr_float(el, "EmissivityFront"),
    )


def _parse_boundary_condition(el: Any) -> ThermBoundaryCondition | None:
    """Parse a <BoundaryCondition> element."""
    name = el.get("Name", "").strip()
    if not name:
        return None
    return ThermBoundaryCondition(
        id=name,  # THERM uses BC Name as identifier
        name=name,
        bc_type=_classify_bc_type(el),
        temperature=_attr_float(el, "Temperature"),
        film_coefficient=_attr_float(el, "H"),
        radiation_model=el.get("RadiationModel"),
    )


def _classify_bc_type(el: Any) -> str:
    """
    Classify a BC element as interior, exterior, or adiabatic.

    THERM doesn't have a single "type" attribute. We classify based on
    the Name attribute conventions used in THERM:
    - Names containing "interior" or "indoor" -> interior
    - Names containing "exterior" or "outdoor" -> exterior
    - Names containing "adiabatic" -> adiabatic
    - Otherwise -> the raw Type attribute or "unknown"
    """
    name = el.get("Name", "").strip().lower()
    if "interior" in name or "indoor" in name:
        return "interior"
    if "exterior" in name or "outdoor" in name:
        return "exterior"
    if "adiabatic" in name:
        return "adiabatic"

    # Fall back to the Type attribute
    bc_type = el.get("Type", "").strip().lower()
    if bc_type:
        return bc_type
    return "unknown"


def _parse_polygon(el: Any, ns: str) -> ThermPolygon | None:
    """Parse a <Polygon> element with child <Point> elements."""
    poly_id = el.get("ID", "").strip()
    material = el.get("Material", "").strip()
    if not poly_id:
        return None

    vertices: list[tuple[float, float]] = []
    for pt in _findall(el, "Point", ns):
        x = _attr_float(pt, "x")
        y = _attr_float(pt, "y")
        if x is not None and y is not None:
            vertices.append((x, y))

    return ThermPolygon(
        id=poly_id,
        material_id=material,
        vertices=vertices,
        name=el.get("Name"),
    )


# ---- XML helpers ----


def _detect_namespace(root: Any) -> str:
    """Detect the namespace from the root element's tag."""
    tag = root.tag
    if tag.startswith("{"):
        return tag[1 : tag.index("}")]
    return ""


def _ns_tag(tag: str, ns: str) -> str:
    """Create a namespaced tag name."""
    if ns:
        return f"{{{ns}}}{tag}"
    return tag


def _find(parent: Any, tag: str, ns: str) -> Any:
    """Find a single child element by tag, namespace-aware."""
    return parent.find(_ns_tag(tag, ns))


def _findall(parent: Any, tag: str, ns: str) -> list[Any]:
    """Find all child elements by tag, namespace-aware."""
    return parent.findall(_ns_tag(tag, ns))


def _get_text(root: Any, tag: str, ns: str) -> str | None:
    """Get text content of a child element."""
    el = _find(root, tag, ns)
    if el is not None and el.text:
        return el.text.strip()
    return None


def _attr_float(el: Any, attr: str) -> float | None:
    """Get a float attribute, returning None if missing or invalid."""
    val = el.get(attr)
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _attr_int(el: Any, attr: str) -> int | None:
    """Get an integer attribute, returning None if missing or invalid."""
    val = el.get(attr)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None

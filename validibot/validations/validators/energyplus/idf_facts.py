"""IDF / epJSON parser for the EnergyPlus validator's step inputs.

This is the proof-of-concept set defined by ADR-2026-05-22 — three facts
extracted from the (resolved) IDF and exposed in the ``i.*`` CEL
namespace for input-stage assertions:

    - ``idf_version``    — string from the IDF ``Version`` object
    - ``zone_count``     — count of IDF ``Zone`` objects
    - ``north_axis_deg`` — number from the ``Building`` object North Axis field

The extractor handles both legacy IDF text format and the JSON-shaped
epJSON variant. For IDF text we use regex-based parsing (lightweight,
no external dependencies, sufficient for the POC scope). For epJSON we
walk the parsed JSON structure.

Phase 2 (per ADR-2026-05-22) will extend the catalog to ~12 facts —
``building_name``, ``terrain``, ``solar_distribution``,
``timestep_per_hour``, ``surface_count``, ``window_count``,
``construction_count``, ``run_period_count``, ``has_hvac``. The
extraction architecture here is designed to scale to that set without
rework.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ── Regex patterns ──────────────────────────────────────────────────
#
# IDF text format is comma-separated, semicolon-terminated, with `!`
# starting line comments. Objects look like:
#
#     Version,
#       25.1;                    !- Version Identifier
#
#     Building,
#       Simple One Zone,         !- Name
#       0.0,                     !- North Axis {deg}
#       Suburbs,                 !- Terrain
#       ...;
#
#     Zone,
#       ZONE ONE,                !- Name
#       ...;
#
# We strip line comments first, then run pattern matches across the
# whole stripped text. This is intentionally lightweight — full IDF
# parsing (eppy etc.) would handle the long tail of edge cases but is
# overkill for three POC facts. Phase 2 may revisit.

_LINE_COMMENT_RE = re.compile(r"!.*?$", re.MULTILINE)
_BLOCK_LINE_COMMENT_RE = re.compile(r"^\s*!-.*?$", re.MULTILINE)

# Match `Version, <version>;` allowing whitespace and newlines between
# the object name, the comma, the value, and the terminating semicolon.
_VERSION_RE = re.compile(
    r"\bVersion\s*,\s*([^;,\s]+)\s*;",
    re.IGNORECASE,
)

# Match the Building object's name field; the North Axis field is
# captured separately because it may be:
#   - present with a numeric value:    Building, MyBuilding, 45, Suburbs;
#   - present but blank:               Building, MyBuilding, , Suburbs;
#   - present with non-numeric junk:   Building, MyBuilding, autodetect;
#   - ABSENT (object terminates):      Building, MyBuilding;
#
# Per the EnergyPlus IDD, the North Axis field defaults to 0.0 in all
# of the latter three cases. The first regex captures just the name
# (matching any Building object); the second optionally captures the
# axis value if present. _extract_north_axis() applies the 0.0 default
# whenever the field is absent, blank, or unparseable.
_BUILDING_NAME_RE = re.compile(
    r"\bBuilding\s*,\s*"  # object header
    r"([^,;]+)\s*[,;]",  # field 1: name, followed by , or ;
    re.IGNORECASE,
)
# Capture only the axis field if a second field exists. Used after
# _BUILDING_NAME_RE confirms a Building object is present.
_BUILDING_AXIS_RE = re.compile(
    r"\bBuilding\s*,\s*"  # object header
    r"[^,;]+\s*,\s*"  # field 1: name (uncaptured)
    r"([^,;]*)\s*[,;]",  # field 2: axis (captured, may be empty)
    re.IGNORECASE,
)

# Count of Zone object declarations. The pattern matches `Zone,` at
# the start of an object (with optional leading whitespace and word
# boundary). We exclude variants like `ZoneList,`, `ZoneInfiltration:*,`,
# `ZoneVentilation:*,` etc. by requiring the comma immediately after
# the bare word "Zone".
_ZONE_RE = re.compile(
    r"(?:^|\n)\s*Zone\s*,",
    re.IGNORECASE,
)


def extract_poc_facts(payload: Any) -> dict[str, Any] | None:
    """Extract the three POC step inputs from an IDF or epJSON payload.

    Returns a dict keyed by catalog ``contract_key`` containing only the
    keys that were successfully extracted. Returns None if the payload
    is neither IDF text nor epJSON dict.

    The dict may be partial — for example, an IDF without a Version
    object will produce a dict without the ``idf_version`` key. The
    catalog's ``on_missing`` policy on each entry determines whether
    such absences are acceptable or trigger a run-time error.

    Args:
        payload: One of:
            - ``str`` or ``bytes`` — treated as raw IDF text
            - ``dict`` — treated as parsed epJSON
            - anything else — returns None

    Returns:
        Dict with any subset of {"idf_version", "zone_count",
        "north_axis_deg"}, or None if the payload format is not
        recognised.
    """
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, AttributeError):
            return None

    if isinstance(payload, str):
        return _extract_from_idf_text(payload)

    if isinstance(payload, dict):
        return _extract_from_epjson(payload)

    # Some pipelines hand us a JSON-encoded string of epJSON content.
    # Try to recover that case before giving up.
    return None


# ── IDF text extraction ──────────────────────────────────────────────


def _extract_from_idf_text(idf_text: str) -> dict[str, Any]:
    """Extract the POC facts from raw IDF text.

    Strips comments first, then runs each fact's pattern. Each
    extraction is independent — failure to parse one field doesn't
    block the others.
    """
    # Strip IDF Editor's auto-generated `!-` comments first (these
    # appear on their own lines and can interfere with multi-line
    # object matching). Then strip any remaining `!` line comments.
    stripped = _BLOCK_LINE_COMMENT_RE.sub("", idf_text)
    stripped = _LINE_COMMENT_RE.sub("", stripped)

    facts: dict[str, Any] = {}

    version = _extract_version(stripped)
    if version is not None:
        facts["idf_version"] = version

    north_axis = _extract_north_axis(stripped)
    if north_axis is not None:
        facts["north_axis_deg"] = north_axis

    facts["zone_count"] = _count_zones(stripped)

    return facts


def _extract_version(idf_text: str) -> str | None:
    """Extract the EnergyPlus version identifier from a Version object."""
    match = _VERSION_RE.search(idf_text)
    if not match:
        return None
    return match.group(1).strip() or None


def _extract_north_axis(idf_text: str) -> float | None:
    """Extract the Building object's North Axis field (degrees).

    Returns ``None`` only when no Building object is present at all.
    When a Building object exists, returns the parsed numeric value
    of the North Axis field; falls back to the EnergyPlus IDD default
    of 0.0 in any of these cases:

      - The Building object terminates after the name
        (``Building, MyBuilding;``)
      - The North Axis field is present but blank
        (``Building, MyBuilding, , Suburbs;``)
      - The North Axis field is present but non-numeric
        (``Building, MyBuilding, autodetect;``)

    The two-step regex approach (name match first, axis match second)
    handles all four cases correctly. A single regex requiring the
    axis field would miss the minimal "name-only" form, which is the
    P3 case the May 2026 review flagged.
    """
    # First, confirm a Building object exists. If not, return None
    # (distinct from 0.0 — the caller's on_missing policy decides
    # what to do when the object itself is absent).
    if not _BUILDING_NAME_RE.search(idf_text):
        return None
    # Building object is present. Try to extract the axis field;
    # fall back to the EnergyPlus default of 0.0 if it's absent,
    # blank, or unparseable.
    axis_match = _BUILDING_AXIS_RE.search(idf_text)
    if not axis_match:
        # Building object has only the name field (terminator after
        # field 1). Per EnergyPlus IDD, North Axis defaults to 0.0.
        return 0.0
    raw = axis_match.group(1).strip()
    if not raw:
        # Field present but blank — IDD default applies.
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        # Field present but unparseable — fall back to the IDD
        # default rather than failing extraction.
        return 0.0


def _count_zones(idf_text: str) -> int:
    """Count Zone object declarations.

    Excludes ZoneList, ZoneInfiltration:*, ZoneVentilation:* and other
    similar object types by requiring the comma immediately after the
    bare word "Zone".
    """
    return len(_ZONE_RE.findall(idf_text))


# ── epJSON extraction ────────────────────────────────────────────────


def _extract_from_epjson(epjson: dict[str, Any]) -> dict[str, Any]:
    """Extract the POC facts from a parsed epJSON dict.

    epJSON top-level structure:
        {
            "Version": {"Version 1": {"version_identifier": "25.1"}},
            "Building": {"My Building": {"north_axis": 0, ...}},
            "Zone": {"Zone One": {...}, "Zone Two": {...}, ...},
            ...
        }

    Object types are top-level keys; instances are second-level keys
    with their fields as the values.
    """
    facts: dict[str, Any] = {}

    # Version: take the first (and typically only) Version entry's
    # version_identifier field.
    version_objs = epjson.get("Version")
    if isinstance(version_objs, dict) and version_objs:
        first_version = next(iter(version_objs.values()))
        if isinstance(first_version, dict):
            version_id = first_version.get("version_identifier")
            if isinstance(version_id, str) and version_id.strip():
                facts["idf_version"] = version_id.strip()

    # Building: take the first Building entry's north_axis field.
    building_objs = epjson.get("Building")
    if isinstance(building_objs, dict) and building_objs:
        first_building = next(iter(building_objs.values()))
        if isinstance(first_building, dict):
            north_axis = first_building.get("north_axis")
            if isinstance(north_axis, (int, float)):
                facts["north_axis_deg"] = float(north_axis)
            elif north_axis is None:
                # Field absent — fall back to EnergyPlus default.
                facts["north_axis_deg"] = 0.0

    # Zone count: number of entries under the "Zone" top-level key.
    zone_objs = epjson.get("Zone")
    if isinstance(zone_objs, dict):
        facts["zone_count"] = len(zone_objs)
    else:
        facts["zone_count"] = 0

    return facts


# ── JSON-encoded epJSON recovery ─────────────────────────────────────
# Some pipelines hand us a JSON-encoded string of epJSON content rather
# than a parsed dict. _extract_from_idf_text above will quickly fail to
# find any IDF-style patterns and return an empty dict. Callers can
# pre-parse if they know the format; we keep extraction simple by
# refusing to guess.


def _try_parse_epjson_string(text: str) -> dict[str, Any] | None:
    """Best-effort recovery of an epJSON dict from a JSON-encoded string.

    Returns None on failure. Currently unused by extract_poc_facts —
    available for callers that know they have epJSON-as-string but
    haven't parsed it yet.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None

"""IDF / epJSON parser for the EnergyPlus validator's step inputs.

Extracts a set of facts from the (resolved) IDF and exposes them in
the ``i.*`` CEL namespace for input-stage assertions. The catalog
spec lives in ``config.py``; this module is the parser side.

Facts produced (validator revision 3, ADR-2026-05-22 Phase 2):

    Building characteristics
    - ``idf_version``         — string from the IDF ``Version`` object
    - ``building_name``       — Building object Name (field 1)
    - ``terrain``             — Building object Terrain (field 3, default ``Suburbs``)
    - ``north_axis_deg``      — Building object North Axis (field 2, default 0.0)
    - ``solar_distribution``  — Building object Solar Distribution (field 6,
                                default ``FullExterior``)

    Simulation configuration
    - ``timestep_per_hour``   — Timestep object Number of Timesteps per Hour
                                (default 4 per IDD)
    - ``run_period_count``    — count of RunPeriod objects

    Geometry counts
    - ``zone_count``          — count of Zone objects
    - ``surface_count``       — count of BuildingSurface:Detailed objects
    - ``window_count``        — count of Window + FenestrationSurface:Detailed
    - ``construction_count``  — count of Construction objects

    Capability flag
    - ``has_hvac``            — True when any of HVACTemplate:*, AirLoopHVAC,
                                ZoneHVAC:* are declared

The extractor handles both legacy IDF text format and the JSON-shaped
epJSON variant. For IDF text we use regex-based parsing (lightweight,
no external dependencies). For epJSON we walk the parsed JSON
structure.

Each fact is extracted independently — failure to parse one doesn't
block the others. The catalog's ``on_missing`` policy on each entry
determines whether absence triggers a run-time error or quietly
falls back to null.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ── Regex patterns ──────────────────────────────────────────────────
#
# IDF text format is comma-separated, semicolon-terminated, with ``!``
# starting line comments. The IDF Editor convention is to use ``!-``
# headers on each line (which we strip first so multi-line object
# matching works cleanly).

_LINE_COMMENT_RE = re.compile(r"!.*?$", re.MULTILINE)
_BLOCK_LINE_COMMENT_RE = re.compile(r"^\s*!-.*?$", re.MULTILINE)

# ── Single-field extractors ────────────────────────────────────────

# ``Version, <version>;`` — allow whitespace and newlines between the
# header, the value, and the semicolon.
_VERSION_RE = re.compile(
    r"\bVersion\s*,\s*([^;,\s]+)\s*;",
    re.IGNORECASE,
)

# ``Timestep, <n>;`` — the per-hour timestep count.
_TIMESTEP_RE = re.compile(
    r"\bTimestep\s*,\s*([^;,\s]+)\s*;",
    re.IGNORECASE,
)

# ── Building object: positional field parser ───────────────────────
# The Building object's full IDD shape is:
#
#   Building,
#       A1, !- Name
#       N1, !- North Axis {deg}
#       A2, !- Terrain
#       N2, !- Loads Convergence Tolerance Value
#       N3, !- Temperature Convergence Tolerance Value {deltaC}
#       A3, !- Solar Distribution
#       N4, !- Maximum Number of Warmup Days
#       N5; !- Minimum Number of Warmup Days
#
# Rather than write one regex per field, we capture the whole object
# body (everything between ``Building,`` and the terminating ``;``)
# once and split on commas. Callers index into the resulting list
# and apply per-field defaults from the IDD.
_BUILDING_BODY_RE = re.compile(
    r"\bBuilding\s*,([^;]*);",
    re.IGNORECASE,
)

# Object-count helper template. The pattern matches ``<ObjectName>,``
# at the start of an object — that is, after whitespace following the
# start of the string or a newline. Crucially the comma must come
# immediately after the bare word, so ``Construction,`` matches but
# ``Construction:CfactorUndergroundWall,`` does not.
_OBJECT_COUNT_TEMPLATE = r"(?:^|\n)\s*{name}\s*,"


def _object_count_re(name: str) -> re.Pattern[str]:
    """Compile a count-pattern for the given object name."""
    return re.compile(
        _OBJECT_COUNT_TEMPLATE.format(name=re.escape(name)),
        re.IGNORECASE,
    )


_ZONE_RE = _object_count_re("Zone")
_BUILDING_SURFACE_RE = _object_count_re("BuildingSurface:Detailed")
_WINDOW_RE = _object_count_re("Window")
_FENESTRATION_SURFACE_RE = _object_count_re("FenestrationSurface:Detailed")
_CONSTRUCTION_RE = _object_count_re("Construction")
_RUN_PERIOD_RE = _object_count_re("RunPeriod")

# HVAC capability flag — presence of any of these object families is
# a strong signal the model has an HVAC system. We don't try to
# enumerate every HVAC object type; one positive match is enough.
_HVAC_PRESENCE_RE = re.compile(
    r"(?:^|\n)\s*(?:HVACTemplate:|AirLoopHVAC|ZoneHVAC:)",
    re.IGNORECASE,
)


# ── EnergyPlus IDD defaults ─────────────────────────────────────────
# Per the EnergyPlus IDD, several Building-object fields default to
# specific values when blank or absent. We mirror those here so the
# extracted facts match what EnergyPlus would actually use.
_BUILDING_DEFAULTS = {
    "north_axis_deg": 0.0,
    "terrain": "Suburbs",
    "solar_distribution": "FullExterior",
}

# Timestep defaults to 4 per hour per the IDD when no Timestep object
# is present.
_TIMESTEP_DEFAULT = 4


def extract_facts(payload: Any) -> dict[str, Any] | None:
    """Extract step input facts from an IDF or epJSON payload.

    Returns a dict keyed by catalog ``contract_key`` containing only
    the facts that were successfully extracted. Returns None if the
    payload is neither IDF text nor an epJSON dict.

    The returned dict may be partial — for example, an IDF without a
    Version object will produce a dict without the ``idf_version``
    key. The catalog's ``on_missing`` policy on each entry determines
    whether such absences are acceptable or trigger a run-time error.

    Args:
        payload: One of:
            - ``str`` or ``bytes`` — treated as raw IDF text
            - ``dict`` — treated as parsed epJSON
            - anything else — returns None

    Returns:
        Dict with any subset of the documented facts, or None if the
        payload format is not recognised.
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

    return None


# Back-compat alias — the original POC name. Several callers and
# tests still reference it; the new name reflects the post-Phase-2
# expanded scope.
extract_poc_facts = extract_facts


# ── IDF text extraction ──────────────────────────────────────────────


def _extract_from_idf_text(idf_text: str) -> dict[str, Any]:
    """Extract all facts from raw IDF text.

    Strips comments first, then runs each fact's extractor. Each
    extraction is independent — failure to parse one field doesn't
    block the others.
    """
    # Strip IDF Editor's auto-generated ``!-`` headers first (they
    # appear on their own lines and can interfere with multi-line
    # object matching). Then strip any remaining ``!`` line comments.
    stripped = _BLOCK_LINE_COMMENT_RE.sub("", idf_text)
    stripped = _LINE_COMMENT_RE.sub("", stripped)

    facts: dict[str, Any] = {}

    version = _extract_version(stripped)
    if version is not None:
        facts["idf_version"] = version

    building_fields = _parse_building_fields(stripped)
    if building_fields is not None:
        name = _building_field(building_fields, 0)
        if name:
            facts["building_name"] = name
        facts["north_axis_deg"] = _building_axis(building_fields)
        facts["terrain"] = _building_string_field(
            building_fields,
            2,
            _BUILDING_DEFAULTS["terrain"],
        )
        facts["solar_distribution"] = _building_string_field(
            building_fields,
            5,
            _BUILDING_DEFAULTS["solar_distribution"],
        )

    timestep = _extract_timestep_per_hour(stripped)
    if timestep is not None:
        facts["timestep_per_hour"] = timestep

    facts["zone_count"] = len(_ZONE_RE.findall(stripped))
    facts["surface_count"] = len(_BUILDING_SURFACE_RE.findall(stripped))
    facts["window_count"] = len(_WINDOW_RE.findall(stripped)) + len(
        _FENESTRATION_SURFACE_RE.findall(stripped)
    )
    facts["construction_count"] = len(_CONSTRUCTION_RE.findall(stripped))
    facts["run_period_count"] = len(_RUN_PERIOD_RE.findall(stripped))
    facts["has_hvac"] = bool(_HVAC_PRESENCE_RE.search(stripped))

    return facts


def _extract_version(idf_text: str) -> str | None:
    """Extract the EnergyPlus version identifier from a Version object."""
    match = _VERSION_RE.search(idf_text)
    if not match:
        return None
    return match.group(1).strip() or None


def _extract_timestep_per_hour(idf_text: str) -> int | None:
    """Extract the Timestep object's per-hour count.

    Returns the parsed integer when a Timestep object is present.
    Returns None when no Timestep object is declared (the IDD default
    of 4 is applied by the catalog's ``on_missing`` policy at
    evaluation time rather than silently injected here — that way
    authors who explicitly assert ``i.timestep_per_hour >= 6`` get
    failure feedback when the IDF omits the object rather than a
    misleading "passed because we made one up" outcome).
    """
    match = _TIMESTEP_RE.search(idf_text)
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _parse_building_fields(idf_text: str) -> list[str] | None:
    """Return the Building object's fields as a stripped list.

    Returns None when no Building object is present. The returned
    list is field-aligned to the IDD layout:

        [0] Name
        [1] North Axis (degrees)
        [2] Terrain
        [3] Loads Convergence Tolerance
        [4] Temperature Convergence Tolerance
        [5] Solar Distribution
        [6] Max Warmup Days
        [7] Min Warmup Days

    Shorter object instances (e.g. ``Building, MyBuilding;``) return
    a 1-element list. Callers must use ``_building_field`` /
    ``_building_string_field`` / ``_building_axis`` to read fields
    by index — those helpers handle short lists and apply IDD
    defaults uniformly.
    """
    match = _BUILDING_BODY_RE.search(idf_text)
    if not match:
        return None
    body = match.group(1)
    return [f.strip() for f in body.split(",")]


def _building_field(fields: list[str], index: int) -> str:
    """Return ``fields[index]`` or empty string if the index is missing."""
    if 0 <= index < len(fields):
        return fields[index]
    return ""


def _building_string_field(
    fields: list[str],
    index: int,
    default: str,
) -> str:
    """Return a string Building field, falling back to the IDD default.

    The default applies when the field is missing entirely (short
    object) OR when it's present but blank (``Building, X, , Y;``).
    Matches what EnergyPlus does at simulation time.
    """
    value = _building_field(fields, index)
    if not value:
        return default
    return value


def _building_axis(fields: list[str]) -> float:
    """Return the Building North Axis as a float, applying the IDD default.

    The IDD specifies North Axis defaults to 0.0 in three cases:
    - Field absent (short Building object)
    - Field present but blank
    - Field present but non-numeric

    Returning 0.0 in all three matches EnergyPlus runtime behaviour
    and lets authors write ``i.north_axis_deg == 0`` without first
    guarding against null. The catalog declares this field with
    ``on_missing="null"`` but in practice we never produce None for
    it (the IDD default makes None a non-occurring outcome).
    """
    raw = _building_field(fields, 1)
    if not raw:
        return _BUILDING_DEFAULTS["north_axis_deg"]
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _BUILDING_DEFAULTS["north_axis_deg"]


# ── epJSON extraction ────────────────────────────────────────────────


def _extract_from_epjson(epjson: dict[str, Any]) -> dict[str, Any]:
    """Extract all facts from a parsed epJSON dict.

    epJSON top-level structure:

        {
            "Version": {"Version 1": {"version_identifier": "25.1"}},
            "Building": {"My Building": {"north_axis": 0, ...}},
            "Zone": {"Zone One": {...}, "Zone Two": {...}, ...},
            ...
        }

    Object types are top-level keys; instances are second-level keys
    with their fields as the values. The instance KEY (not the
    ``name`` field, which doesn't exist in epJSON) carries the IDF
    Name; we read it for ``building_name``.
    """
    facts: dict[str, Any] = {}

    # Version: take the first (typically only) Version entry's
    # version_identifier field.
    version_objs = epjson.get("Version")
    if isinstance(version_objs, dict) and version_objs:
        first_version = next(iter(version_objs.values()))
        if isinstance(first_version, dict):
            version_id = first_version.get("version_identifier")
            if isinstance(version_id, str) and version_id.strip():
                facts["idf_version"] = version_id.strip()

    # Building: name is the instance key; other fields are inside.
    building_objs = epjson.get("Building")
    if isinstance(building_objs, dict) and building_objs:
        first_key, first_building = next(iter(building_objs.items()))
        if isinstance(first_key, str) and first_key.strip():
            facts["building_name"] = first_key.strip()
        if isinstance(first_building, dict):
            facts["north_axis_deg"] = _epjson_number(
                first_building.get("north_axis"),
                default=_BUILDING_DEFAULTS["north_axis_deg"],
            )
            facts["terrain"] = _epjson_string(
                first_building.get("terrain"),
                default=_BUILDING_DEFAULTS["terrain"],
            )
            facts["solar_distribution"] = _epjson_string(
                first_building.get("solar_distribution"),
                default=_BUILDING_DEFAULTS["solar_distribution"],
            )

    # Timestep: same shape — instance dict with one numeric field.
    timestep_objs = epjson.get("Timestep")
    if isinstance(timestep_objs, dict) and timestep_objs:
        first_timestep = next(iter(timestep_objs.values()))
        if isinstance(first_timestep, dict):
            n = first_timestep.get("number_of_timesteps_per_hour")
            if isinstance(n, (int, float)):
                facts["timestep_per_hour"] = int(n)

    # Object counts.
    facts["zone_count"] = _epjson_count(epjson, "Zone")
    facts["surface_count"] = _epjson_count(epjson, "BuildingSurface:Detailed")
    facts["window_count"] = _epjson_count(epjson, "Window") + _epjson_count(
        epjson, "FenestrationSurface:Detailed"
    )
    facts["construction_count"] = _epjson_count(epjson, "Construction")
    facts["run_period_count"] = _epjson_count(epjson, "RunPeriod")

    # HVAC presence: any top-level key starting with one of the
    # known HVAC family prefixes counts.
    facts["has_hvac"] = any(
        key.startswith(("HVACTemplate:", "ZoneHVAC:")) or key == "AirLoopHVAC"
        for key in epjson
        if isinstance(key, str)
    )

    return facts


def _epjson_count(epjson: dict[str, Any], object_type: str) -> int:
    """Return the number of instances of ``object_type`` in the epJSON dict."""
    entries = epjson.get(object_type)
    if isinstance(entries, dict):
        return len(entries)
    return 0


def _epjson_string(value: Any, *, default: str) -> str:
    """Return value as a stripped string, or default when missing/blank."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _epjson_number(value: Any, *, default: float) -> float:
    """Return value as a float, or default when missing/unparseable."""
    if isinstance(value, (int, float)):
        return float(value)
    return default


# ── JSON-encoded epJSON recovery ─────────────────────────────────────
# Some pipelines hand us a JSON-encoded string of epJSON content rather
# than a parsed dict. The text extractor will fail to find any IDF-style
# patterns and return an empty dict; callers can pre-parse if they know
# the format. The helper below is available for that case.


def _try_parse_epjson_string(text: str) -> dict[str, Any] | None:
    """Best-effort recovery of an epJSON dict from a JSON-encoded string.

    Returns None on failure. Not used by ``extract_facts`` — available
    for callers that know they have epJSON-as-string but haven't
    parsed it yet.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None

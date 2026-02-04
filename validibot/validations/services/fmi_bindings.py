"""
Input binding resolution helpers for FMI validators.

Bindings are defined on ValidatorCatalogEntry.input_binding_path. When absent,
we default to matching the catalog slug against a top-level submission key.
"""

from __future__ import annotations

from typing import Any


def resolve_input_value(
    payload: Any,
    *,
    binding_path: str | None,
    slug: str,
) -> Any:
    """
    Resolve a single input value from a submission payload.

    Supports dotted-path traversal for nested dictionaries. If no binding_path
    is provided, falls back to looking up the catalog slug as a top-level key.
    """
    if not isinstance(payload, dict):
        return None

    path_to_use = (binding_path or "").strip() or slug
    parts = [p for p in path_to_use.split(".") if p]

    cursor: Any = payload
    for part in parts:
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor.get(part)
    return cursor

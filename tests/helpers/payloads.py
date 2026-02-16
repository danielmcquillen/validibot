"""Shared test payload generators for validation tests."""

from __future__ import annotations

from typing import Any


def valid_product_payload() -> dict[str, Any]:
    """Produce a sample product payload that satisfies the example JSON Schema."""
    return {
        "sku": "ABCD1234",
        "name": "Widget Mini",
        "price": 19.99,
        "rating": 95,
        "tags": ["gadgets", "mini"],
        "dimensions": {"width": 3.5, "height": 1.2},
        "in_stock": True,
    }


def invalid_product_payload() -> dict[str, Any]:
    """Produce a payload that intentionally violates the schema (rating > 100)."""
    bad = valid_product_payload()
    bad["rating"] = 150  # violates max 100
    return bad

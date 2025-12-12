from __future__ import annotations

import json
from pathlib import Path

import pytest
from rest_framework.test import APIClient

BASE_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _ensure_billing_plans(db) -> None:
    """
    Ensure billing Plans exist for tests that create users/orgs.

    User creation triggers trial subscription setup which requires STARTER plan.
    """
    from validibot.billing.constants import PlanCode
    from validibot.billing.models import Plan

    Plan.objects.get_or_create(
        code=PlanCode.STARTER,
        defaults={
            "name": "Starter",
            "basic_launches_limit": 10000,
            "included_credits": 200,
            "max_seats": 2,
            "max_workflows": 10,
            "monthly_price_cents": 2900,
            "display_order": 1,
        },
    )
    Plan.objects.get_or_create(
        code=PlanCode.TEAM,
        defaults={
            "name": "Team",
            "basic_launches_limit": 100000,
            "included_credits": 1000,
            "max_seats": 10,
            "max_workflows": 100,
            "monthly_price_cents": 9900,
            "display_order": 2,
        },
    )
    Plan.objects.get_or_create(
        code=PlanCode.ENTERPRISE,
        defaults={
            "name": "Enterprise",
            "basic_launches_limit": 1000000,
            "included_credits": 5000,
            "max_seats": 100,
            "max_workflows": 1000,
            "monthly_price_cents": 0,
            "display_order": 3,
        },
    )


def _locate_schema(rel_path: str) -> Path:
    """Return the preferred path for XML schemas, falling back to legacy dirs."""
    primary = BASE_DIR / "assets" / "xml" / "schemas" / rel_path
    if primary.exists():
        return primary
    # Legacy fallbacks kept for older fixtures/tests
    legacy_dirs = [
        BASE_DIR / "assets" / "xsd",
        BASE_DIR / "assets" / "rng",
        BASE_DIR / "assets" / "dtd",
    ]
    for candidate_root in legacy_dirs:
        candidate = candidate_root / rel_path
        if candidate.exists():
            return candidate
    return primary


@pytest.fixture
def load_xml_asset():
    """
    Load a valid XML asset relative to tests/assets.
    Usage: load_valid_xml_asset("example_product.xml")
    """

    def _loader(rel_path: str) -> str:
        path = BASE_DIR / "assets" / "xml" / rel_path
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def load_json_asset():
    """
    Load a JSON asset relative to tests/assets.
    Usage: load_json_asset("example_product_schema.json")
    """

    def _loader(rel_path: str):
        path = BASE_DIR / "assets" / "json" / rel_path
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    return _loader


@pytest.fixture
def load_xsd_asset():
    """
    Load an XSD asset relative to tests/assets.
    Usage: load_xsd_asset("example_product_schema.xsd")
    """

    def _loader(rel_path: str):
        path = _locate_schema(rel_path)
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def load_rng_asset():
    """
    Load an RNG asset relative to tests/assets.
    Usage: load_rng_asset("example_product_schema.rng")
    """

    def _loader(rel_path: str) -> str:
        path = _locate_schema(rel_path)
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def load_dtd_asset():
    """Load a DTD asset relative to tests/assets."""

    def _loader(rel_path: str) -> str:
        path = _locate_schema(rel_path)
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()

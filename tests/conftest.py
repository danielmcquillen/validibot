from __future__ import annotations

import json
from pathlib import Path

import pytest
from rest_framework.test import APIClient

BASE_DIR = Path(__file__).resolve().parent


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
        path = BASE_DIR / "assets" / "xsd" / rel_path
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
        path = BASE_DIR / "assets" / "rng" / rel_path
        with path.open("r", encoding="utf-8") as f:
            return f.read()

    return _loader


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture(autouse=True)
def celery_eager(settings):
    """
    Run Celery tasks synchronously during tests.
    """
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True

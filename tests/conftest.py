
from __future__ import annotations

import json
from pathlib import Path

import pytest
from rest_framework.test import APIClient

BASE_DIR = Path(__file__).resolve().parent


@pytest.fixture
def load_json_asset():
    """
    Load a JSON asset relative to tests/assets.
    Usage: load_json_asset("json/example_product_schema.json")
    """
    def _loader(rel_path: str):
        path = BASE_DIR / "assets" / rel_path
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
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

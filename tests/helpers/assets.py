"""Shared test asset loaders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings


def load_test_asset(relative_path: str) -> bytes:
    """Load a test asset file from the tests directory."""
    asset_path = Path(settings.BASE_DIR) / "tests" / relative_path
    return asset_path.read_bytes()


def load_json_test_asset(relative_path: str) -> dict[str, Any]:
    """Load and parse a JSON test asset."""
    data = load_test_asset(relative_path)
    return json.loads(data)

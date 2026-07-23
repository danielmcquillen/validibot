"""Protect the published ``validibot-shared`` dependency boundary.

Validibot is distributed as source and built in several environments. All of
them must resolve the same published shared-contract wheel from the committed
lockfile. A committed sibling-path override would make host tests exercise
different code and would make standalone Docker builds depend on a directory
outside their build context.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE = REPO_ROOT / "pyproject.toml"
LOCK_FILE = REPO_ROOT / "uv.lock"
SHARED_PACKAGE_NAME = "validibot-shared"
PYPI_SIMPLE_INDEX = "https://pypi.org/simple"


def _project_metadata() -> dict:
    """Load the application dependency declaration."""
    return tomllib.loads(PROJECT_FILE.read_text(encoding="utf-8"))


def _shared_requirement(project: dict) -> str:
    """Return the single shared-package requirement from project metadata."""
    matches = [
        dependency
        for dependency in project["project"]["dependencies"]
        if dependency.startswith(SHARED_PACKAGE_NAME)
    ]
    assert len(matches) == 1
    return matches[0]


def test_shared_dependency_is_an_exact_published_requirement() -> None:
    """Release builds need one immutable shared-contract version."""
    project = _project_metadata()
    requirement = _shared_requirement(project)

    package_name, separator, version = requirement.partition("==")

    assert package_name == SHARED_PACKAGE_NAME
    assert separator == "=="
    assert version
    assert SHARED_PACKAGE_NAME not in project.get("tool", {}).get("uv", {}).get(
        "sources",
        {},
    )


def test_shared_dependency_lock_entry_uses_pypi() -> None:
    """Frozen installs must consume the published wheel, not a sibling path."""
    project = _project_metadata()
    expected_version = _shared_requirement(project).partition("==")[2]
    lock = tomllib.loads(LOCK_FILE.read_text(encoding="utf-8"))
    matches = [
        package for package in lock["package"] if package["name"] == SHARED_PACKAGE_NAME
    ]

    assert len(matches) == 1
    assert matches[0]["version"] == expected_version
    assert matches[0]["source"] == {"registry": PYPI_SIMPLE_INDEX}
    assert matches[0].get("wheels")

"""Protect the local-editable versus published-package dependency boundary.

Host development intentionally imports ``validibot-shared`` from its sibling
checkout. Docker images cannot see that sibling repository, so their dependency
layer must skip the editable lock entry and install the exact public project pin.
These tests prevent a future Dockerfile cleanup from silently restoring the
missing-path build failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "compose" / "common" / "install-published-shared.sh"
DOCKERFILES = (
    REPO_ROOT / "compose" / "local" / "django" / "Dockerfile",
    REPO_ROOT / "compose" / "production" / "django" / "Dockerfile",
)


def test_published_shared_installer_uses_the_exact_project_pin(tmp_path: Path) -> None:
    """The image helper must pass the declared shared version to uv unchanged."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture_path = tmp_path / "uv-arguments.txt"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$VALIDIBOT_TEST_UV_CAPTURE"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "VALIDIBOT_TEST_UV_CAPTURE": str(capture_path),
    }

    subprocess.run(  # noqa: S603 -- executable and arguments are test-owned.
        [
            "/bin/sh",
            str(INSTALLER),
            sys.executable,
            str(REPO_ROOT / "pyproject.toml"),
        ],
        check=True,
        env=environment,
    )

    project = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    shared_requirement = next(
        dependency
        for dependency in project["project"]["dependencies"]
        if dependency.startswith("validibot-shared==")
    )
    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        "pip",
        "install",
        "--python",
        sys.executable,
        shared_requirement,
    ]


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_dockerfiles_replace_the_host_only_editable_shared_source(
    dockerfile: Path,
) -> None:
    """Every application image must install shared from the published pin."""
    contents = dockerfile.read_text(encoding="utf-8")

    assert "--no-install-package validibot-shared" in contents
    assert "install-published-shared" in contents

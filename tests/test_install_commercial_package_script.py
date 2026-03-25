"""Tests for the commercial package install helper script."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "compose" / "common" / "install-commercial-package.sh"
TEST_PYTHON_BIN = "/opt/validibot/fake-python"


class InstallCommercialPackageScriptTests(SimpleTestCase):
    """Verify the Docker build helper only accepts exact commercial package refs."""

    def _run_script(
        self,
        commercial_package: str,
        private_index_url: str = "",
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        """Run the helper with a fake uv binary so tests can inspect arguments."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            args_file = temp_path / "uv-args.txt"
            fake_uv = temp_path / "uv"
            fake_uv.write_text(
                '#!/bin/sh\nset -eu\nprintf \'%s\\n\' "$@" > "$UV_ARGS_FILE"\n',
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{temp_dir}:{env.get('PATH', '')}"
            env["UV_ARGS_FILE"] = str(args_file)
            env["VALIDIBOT_COMMERCIAL_PACKAGE"] = commercial_package
            env["VALIDIBOT_PRIVATE_INDEX_URL"] = private_index_url

            result = subprocess.run(  # noqa: S603
                ["/bin/sh", str(SCRIPT_PATH), TEST_PYTHON_BIN],
                capture_output=True,
                check=False,
                env=env,
                text=True,
            )
            if args_file.exists():
                uv_args = args_file.read_text(encoding="utf-8").splitlines()
            else:
                uv_args = []
            return result, uv_args

    def test_accepts_exact_version_from_private_index(self):
        """Pinned package specs keep the build on a single known commercial release."""
        result, uv_args = self._run_script(
            commercial_package="validibot-pro==0.1.0",
            private_index_url="https://license@example.com@pypi.validibot.com/simple/",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            uv_args,
            [
                "pip",
                "install",
                "--python",
                TEST_PYTHON_BIN,
                "--index",
                "https://license@example.com@pypi.validibot.com/simple/",
                "validibot-pro==0.1.0",
            ],
        )

    def test_accepts_exact_wheel_url_with_sha256(self):
        """Wheel URLs are accepted only when they target a hashed trusted artifact."""
        result, uv_args = self._run_script(
            commercial_package=(
                "https://customer:apikey@pypi.validibot.com/packages/"
                "validibot_pro-0.1.0-py3-none-any.whl"
                "#sha256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            ),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            uv_args,
            [
                "pip",
                "install",
                "--python",
                TEST_PYTHON_BIN,
                (
                    "https://customer:apikey@pypi.validibot.com/packages/"
                    "validibot_pro-0.1.0-py3-none-any.whl"
                    "#sha256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                ),
            ],
        )

    def test_rejects_untrusted_private_index(self):
        """Pinned package installs must resolve from the trusted private index host."""
        result, uv_args = self._run_script(
            commercial_package="validibot-pro==0.1.0",
            private_index_url="https://license@example.com/simple/",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("must use host pypi.validibot.com", result.stderr)
        self.assertEqual(uv_args, [])

    def test_rejects_floating_package_names(self):
        """Unversioned package names are refused so builds cannot drift silently."""
        result, uv_args = self._run_script(
            commercial_package="validibot-pro",
            private_index_url="https://license@example.com@pypi.validibot.com/simple/",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Floating package names", result.stderr)
        self.assertEqual(uv_args, [])

    def test_rejects_wheel_url_without_sha256(self):
        """Artifact URLs need a SHA-256 fragment so the installer can verify bytes."""
        result, uv_args = self._run_script(
            commercial_package=(
                "https://customer:apikey@pypi.validibot.com/packages/"
                "validibot_pro-0.1.0-py3-none-any.whl"
            ),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("sha256", result.stderr)
        self.assertEqual(uv_args, [])

    def test_rejects_wheel_url_on_untrusted_host(self):
        """Wheel installs must stay on the trusted package host."""
        result, uv_args = self._run_script(
            commercial_package=(
                "https://customer:apikey@example.com/packages/"
                "validibot_pro-0.1.0-py3-none-any.whl"
                "#sha256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            ),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("pypi.validibot.com", result.stderr)
        self.assertEqual(uv_args, [])

    def test_rejects_pinned_package_without_private_index(self):
        """Pinned specs still need the private index URL to resolve the wheel."""
        result, uv_args = self._run_script(
            commercial_package="validibot-enterprise==0.1.0",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("VALIDIBOT_PRIVATE_INDEX_URL must be set", result.stderr)
        self.assertEqual(uv_args, [])

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
PRODUCTION_DOCKERFILE = (
    REPO_ROOT / "compose" / "production" / "django" / "Dockerfile"
).read_text(encoding="utf-8")
LOCAL_DOCKERFILE = (
    REPO_ROOT / "compose" / "local" / "django" / "Dockerfile"
).read_text(encoding="utf-8")
PRODUCTION_COMPOSE = (REPO_ROOT / "docker-compose.production.yml").read_text(
    encoding="utf-8",
)
LOCAL_COMPOSE = (REPO_ROOT / "docker-compose.local.yml").read_text(
    encoding="utf-8",
)


class InstallCommercialPackageScriptTests(SimpleTestCase):
    """Verify the Docker build helper only accepts exact commercial package refs."""

    def _run_script(
        self,
        commercial_package: str,
        *,
        netrc_content: str = (
            "machine pypi.validibot.com\n"
            "  login customer@example.com\n"
            "  password vbp_1_test\n"
        ),
        netrc_mode: int = 0o600,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        """Run with fake uv and an isolated home containing the build secret."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            args_file = temp_path / "uv-args.txt"
            netrc_path = temp_path / ".netrc"
            netrc_path.write_text(netrc_content, encoding="utf-8")
            netrc_path.chmod(netrc_mode)
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
            env["HOME"] = temp_dir

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
                "https://pypi.validibot.com/simple/",
                "validibot-pro==0.1.0",
            ],
        )

    def test_accepts_exact_wheel_url_with_sha256(self):
        """Wheel URLs are accepted only when they target a hashed trusted artifact."""
        result, uv_args = self._run_script(
            commercial_package=(
                "https://pypi.validibot.com/packages/"
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
                    "https://pypi.validibot.com/packages/"
                    "validibot_pro-0.1.0-py3-none-any.whl"
                    "#sha256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                ),
            ],
        )

    def test_rejects_netrc_without_trusted_host(self):
        """The build secret must contain credentials for the trusted host."""
        result, uv_args = self._run_script(
            commercial_package="validibot-pro==0.1.0",
            netrc_content=(
                "machine example.com\n"
                "  login customer@example.com\n"
                "  password vbp_1_test\n"
            ),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("must contain login and password", result.stderr)
        self.assertEqual(uv_args, [])

    def test_rejects_floating_package_names(self):
        """Unversioned package names are refused so builds cannot drift silently."""
        result, uv_args = self._run_script(
            commercial_package="validibot-pro",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Floating package names", result.stderr)
        self.assertEqual(uv_args, [])

    def test_rejects_wheel_url_without_sha256(self):
        """Artifact URLs need a SHA-256 fragment so the installer can verify bytes."""
        result, uv_args = self._run_script(
            commercial_package=(
                "https://pypi.validibot.com/packages/"
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

    def test_rejects_group_readable_netrc(self):
        """Build secrets must not be readable by other container users."""
        result, uv_args = self._run_script(
            commercial_package="validibot-enterprise==0.1.0",
            netrc_mode=0o640,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("must not be accessible by group or other users", result.stderr)
        self.assertEqual(uv_args, [])

    def test_rejects_credentials_embedded_in_wheel_url(self):
        """Raw package keys must never be accepted through image metadata."""
        result, uv_args = self._run_script(
            commercial_package=(
                "https://customer:apikey@pypi.validibot.com/packages/"
                "validibot_pro-0.1.0-py3-none-any.whl"
                "#sha256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            ),
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("pypi.validibot.com", result.stderr)
        self.assertEqual(uv_args, [])


class CommercialPackageBuildSecretContractTests(SimpleTestCase):
    """Keep package credentials out of Docker metadata and durable layers."""

    def test_dockerfiles_mount_netrc_only_for_install_step(self):
        """Both image paths must consume the credential as a BuildKit secret."""
        for dockerfile in (PRODUCTION_DOCKERFILE, LOCAL_DOCKERFILE):
            self.assertIn(
                "--mount=type=secret,id=validibot_commercial_netrc,"
                "target=/root/.netrc,required=false,mode=0600",
                dockerfile,
            )
            self.assertNotIn("ARG VALIDIBOT_PRIVATE_INDEX_URL", dockerfile)

    def test_compose_passes_only_credential_free_reference_as_build_arg(self):
        """Compose must mount netrc rather than serialize it into image args."""
        for compose_file in (PRODUCTION_COMPOSE, LOCAL_COMPOSE):
            self.assertIn("VALIDIBOT_COMMERCIAL_PACKAGE:", compose_file)
            self.assertIn("validibot_commercial_netrc", compose_file)
            self.assertIn("VALIDIBOT_COMMERCIAL_NETRC", compose_file)
            self.assertNotIn("VALIDIBOT_PRIVATE_INDEX_URL", compose_file)

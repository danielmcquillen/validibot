"""Negative-control tests for ADR-2026-04-27 ``[trust-#4]`` isolation.

Slice 4 of the trust-ADR Phase 1 plan. These tests exist *specifically*
to guard against the regression that the rest of Phase 1 was built to
fix: the local Docker runner used to mount the entire
``DATA_STORAGE_ROOT`` read-write into every validator container, so any
container could read or mutate any other run's files.

The tests assert two properties of the ``volumes`` dict the runner
hands to the Docker SDK:

1. **When a workspace is provided**, the dict contains exactly two
   entries — the per-run ``input/`` directory mounted read-only at
   ``/validibot/input``, and the per-run ``output/`` directory mounted
   read-write at ``/validibot/output``. Nothing else; in particular,
   no entry for the global ``DATA_STORAGE_ROOT``.
2. **When a workspace is not provided**, the runner falls back to the
   legacy global mount and logs a warning. This path remains for tests
   and partially-migrated callers but is not a supported production
   configuration after Phase 1; the warning is the regression signal.

We do not run a real container here — the unit-test layer asserts the
configuration is correct, and Docker enforces what's in the
configuration. A full filesystem-walk integration test inside a real
container is the manual smoke step in Slice 5; it would require a
running Docker daemon and a published test image, neither of which fit
the unit-test surface.

What a regression in this file would look like
----------------------------------------------

If a future change re-introduces a global mount, the assertion
``DATA_STORAGE_ROOT not in volumes`` fails with a clear message
pointing at the runner. If a future change accidentally mounts
``input/`` read-write, the mode assertion fails — the container could
then mutate its own input. If the workspace branch is bypassed
silently, the legacy fallback would run instead and the warning-log
test catches it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from validibot.validations.services.run_workspace import CONTAINER_INPUT_DIR
from validibot.validations.services.run_workspace import CONTAINER_OUTPUT_DIR
from validibot.validations.services.run_workspace import MaterializedFile
from validibot.validations.services.run_workspace import RunWorkspace

# ── Mock Docker SDK harness ─────────────────────────────────────────────


def _make_mock_docker_client():
    """Build a mocked Docker SDK client suitable for runner unit tests.

    Mirrors the pattern in ``test_container_cleanup.py``. The
    ``containers.run`` call returns a fake container that "succeeds"
    with exit code 0, so the runner's full happy-path code is
    exercised including the volume-building branches.
    """
    mock_docker = MagicMock()
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.ping.return_value = True

    fake_container = MagicMock()
    fake_container.short_id = "test12345"
    fake_container.wait.return_value = {"StatusCode": 0}
    fake_container.logs.return_value = b""
    mock_client.containers.run.return_value = fake_container

    return mock_docker, mock_client


def _make_workspace(tmp_path: Path) -> RunWorkspace:
    """Construct a minimal RunWorkspace pointing at synthetic host paths.

    The runner only reads ``host_input_dir`` / ``host_output_dir`` and
    the container path constants from the workspace, so we don't need
    real materialised files here — the tests assert on the *volumes
    dict* the runner builds, not on the contents of the dirs.
    """
    host_input = tmp_path / "runs" / "org-1" / "run-aaa" / "input"
    host_output = tmp_path / "runs" / "org-1" / "run-aaa" / "output"
    host_input.mkdir(parents=True, exist_ok=True)
    host_output.mkdir(parents=True, exist_ok=True)
    return RunWorkspace(
        run_id="run-aaa",
        org_id="org-1",
        host_input_dir=host_input,
        host_output_dir=host_output,
        primary_file=MaterializedFile(
            name="model.idf",
            host_path=host_input / "model.idf",
            container_uri=f"file://{CONTAINER_INPUT_DIR}/model.idf",
        ),
    )


# ── Workspace-aware mount tests (the core regression fence) ─────────────


class TestWorkspaceMounts:
    """The volumes dict, when a workspace is provided, must mount only
    the per-run input (ro) and output (rw) directories. Nothing else.
    A regression here means we re-introduced cross-run filesystem
    visibility — exactly the bug Phase 1 exists to fix."""

    def test_input_directory_is_mounted_read_only(self, tmp_path):
        """The container's ``/validibot/input`` mount must be read-only.
        Without this, a buggy validator could write into its own input
        directory, defeating the read-only invariant the ADR specifies."""
        mock_docker, mock_client = _make_mock_docker_client()
        workspace = _make_workspace(tmp_path)

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client  # bypass lazy init

            runner.run(
                container_image="test:latest",
                input_uri=workspace.input_envelope_container_uri,
                output_uri=workspace.output_envelope_container_uri,
                workspace=workspace,
            )

        volumes = mock_client.containers.run.call_args[1]["volumes"]
        input_entry = volumes[str(workspace.host_input_dir)]
        assert input_entry["bind"] == CONTAINER_INPUT_DIR
        assert input_entry["mode"] == "ro", (
            "input/ must be read-only or the read-only invariant fails"
        )

    def test_output_directory_is_mounted_read_write(self, tmp_path):
        """The container's ``/validibot/output`` mount must be
        read-write. Without this, the validator can't write its
        ``output.json`` and every run fails."""
        mock_docker, mock_client = _make_mock_docker_client()
        workspace = _make_workspace(tmp_path)

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            runner.run(
                container_image="test:latest",
                input_uri=workspace.input_envelope_container_uri,
                output_uri=workspace.output_envelope_container_uri,
                workspace=workspace,
            )

        volumes = mock_client.containers.run.call_args[1]["volumes"]
        output_entry = volumes[str(workspace.host_output_dir)]
        assert output_entry["bind"] == CONTAINER_OUTPUT_DIR
        assert output_entry["mode"] == "rw"

    def test_no_global_storage_root_mount_when_workspace_provided(
        self, tmp_path, settings
    ):
        """The central regression fence: when a workspace is provided,
        there must be NO mount of ``DATA_STORAGE_ROOT``. Re-introducing
        the global mount is the literal regression Phase 1 prevents.

        We set DATA_STORAGE_ROOT to a synthetic path and assert it does
        not appear as a key in the volumes dict, regardless of where
        the workspace lives."""
        settings.DATA_STORAGE_ROOT = "/should/never/be/mounted"
        mock_docker, mock_client = _make_mock_docker_client()
        workspace = _make_workspace(tmp_path)

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            runner.run(
                container_image="test:latest",
                input_uri=workspace.input_envelope_container_uri,
                output_uri=workspace.output_envelope_container_uri,
                workspace=workspace,
            )

        volumes = mock_client.containers.run.call_args[1]["volumes"]
        assert "/should/never/be/mounted" not in volumes, (
            "regression: workspace branch leaked the global DATA_STORAGE_ROOT mount"
        )

    def test_workspace_mount_dict_has_exactly_two_entries(self, tmp_path):
        """The volumes dict, with a workspace provided, must contain
        exactly two entries: input and output. A third entry would
        indicate either an extra mount the runner shouldn't add or a
        leakage of the legacy global mount."""
        mock_docker, mock_client = _make_mock_docker_client()
        workspace = _make_workspace(tmp_path)

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            runner.run(
                container_image="test:latest",
                input_uri=workspace.input_envelope_container_uri,
                output_uri=workspace.output_envelope_container_uri,
                workspace=workspace,
            )

        volumes = mock_client.containers.run.call_args[1]["volumes"]
        assert len(volumes) == 2, (  # noqa: PLR2004 — pinning the contract: input + output, nothing else
            f"expected exactly 2 mounts (input + output), got {len(volumes)}: "
            f"{list(volumes.keys())}"
        )
        assert str(workspace.host_input_dir) in volumes
        assert str(workspace.host_output_dir) in volumes

    def test_tmpfs_for_tmp_is_still_configured(self, tmp_path):
        """Phase 1 keeps the tmpfs ``/tmp`` mount because EnergyPlus
        and FMU backends use it for their scratch directories. This
        test pins the tmpfs config so a future mount refactor can't
        accidentally drop it — losing tmpfs would break every advanced
        validator that copies inputs to ``/tmp`` before processing."""
        mock_docker, mock_client = _make_mock_docker_client()
        workspace = _make_workspace(tmp_path)

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            runner.run(
                container_image="test:latest",
                input_uri=workspace.input_envelope_container_uri,
                output_uri=workspace.output_envelope_container_uri,
                workspace=workspace,
            )

        call_kwargs = mock_client.containers.run.call_args[1]
        # ``/tmp`` here is the container path (the tmpfs mountpoint
        # inside the validator container), not a host tempfile path —
        # but ruff S108 fires anyway, so silence it explicitly.
        assert "/tmp" in call_kwargs["tmpfs"]  # noqa: S108


# ── Legacy fallback (workspace=None) ────────────────────────────────────


class TestLegacyFallback:
    """When ``run()`` is called without a workspace, the runner falls
    back to the legacy global mount. The fallback exists for partially
    migrated callers and tests, but it is *not* a supported production
    configuration after Phase 1 — the warning is the regression signal
    that something didn't get updated."""

    def test_warning_logged_when_workspace_omitted(self, caplog, settings):
        """A caller that doesn't pass a workspace should see a warning
        in the logs explaining the regression. Without this signal, a
        partially-migrated dispatch path could silently fall back to
        the bug Phase 1 was meant to fix."""
        settings.DATA_STORAGE_ROOT = "/tmp/legacy-mount-test"  # noqa: S108
        mock_docker, mock_client = _make_mock_docker_client()

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            with caplog.at_level("WARNING"):
                runner.run(
                    container_image="test:latest",
                    input_uri="file:///some/legacy/input.json",
                    output_uri="file:///some/legacy/output.json",
                )

        warning_messages = [
            r.message for r in caplog.records if r.levelname == "WARNING"
        ]
        assert any("workspace" in m and "legacy" in m for m in warning_messages), (
            "expected a 'workspace omitted, legacy fallback' warning; "
            f"got {warning_messages}"
        )

    def test_legacy_fallback_uses_global_storage_root(self, settings):
        """When no workspace is provided, the legacy mount path must
        still produce a working dispatch. Otherwise existing tests
        that haven't been migrated would break — the fallback exists
        precisely to keep them working until they are."""
        settings.DATA_STORAGE_ROOT = "/tmp/legacy-mount-test"  # noqa: S108
        mock_docker, mock_client = _make_mock_docker_client()

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            runner.run(
                container_image="test:latest",
                input_uri="file:///some/input.json",
                output_uri="file:///some/output.json",
            )

        volumes = mock_client.containers.run.call_args[1].get("volumes")
        # The legacy fallback uses DATA_STORAGE_ROOT as both source and
        # bind — that's the original (pre-Phase-1) behaviour we kept
        # intentionally for unmigrated callers.
        assert volumes is not None
        assert "/tmp/legacy-mount-test" in volumes  # noqa: S108


# ── DinD path translation ──────────────────────────────────────────────


class TestDinDPathTranslation:
    """Docker-in-Docker path translation is the only place the Phase 1
    mount strategy gets clever. The worker container sees workspace
    paths under its ``storage_mount_path``, but the Docker daemon
    binds against the host filesystem. The runner introspects the
    named volume's ``Mountpoint`` attribute to translate.

    These tests pin the translation behaviour because a regression
    here would silently mount the wrong host path — the container
    would see an empty input directory instead of its actual run
    inputs, and the run would fail with a confusing 'envelope not
    found' error rather than a clean signal."""

    def test_dind_translation_rebases_worker_path_to_host_path(self, tmp_path):
        """When ``storage_volume`` is set, the runner must rebase the
        worker-side workspace paths to the volume's host mountpoint.
        Otherwise the Docker daemon — which lives on the host, outside
        the worker's mount namespace — can't find the directories."""
        mock_docker, mock_client = _make_mock_docker_client()

        # Simulate the Docker SDK returning a known mountpoint for the
        # named volume.
        fake_volume = MagicMock()
        fake_volume.attrs = {"Mountpoint": "/var/lib/docker/volumes/test_storage/_data"}
        mock_client.volumes.get.return_value = fake_volume

        # Build a workspace whose host_input/host_output live under the
        # storage mount path that the worker sees.
        worker_input = Path("/app/storage/runs/org-1/run-aaa/input")
        worker_output = Path("/app/storage/runs/org-1/run-aaa/output")
        workspace = RunWorkspace(
            run_id="run-aaa",
            org_id="org-1",
            host_input_dir=worker_input,
            host_output_dir=worker_output,
            primary_file=MaterializedFile(
                name="model.idf",
                host_path=worker_input / "model.idf",
                container_uri=f"file://{CONTAINER_INPUT_DIR}/model.idf",
            ),
        )

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner(
                storage_volume="test_storage",
                storage_mount_path="/app/storage",
            )
            runner._client = mock_client

            runner.run(
                container_image="test:latest",
                input_uri=workspace.input_envelope_container_uri,
                output_uri=workspace.output_envelope_container_uri,
                workspace=workspace,
            )

        volumes = mock_client.containers.run.call_args[1]["volumes"]

        expected_host_input = (
            "/var/lib/docker/volumes/test_storage/_data/runs/org-1/run-aaa/input"
        )
        expected_host_output = (
            "/var/lib/docker/volumes/test_storage/_data/runs/org-1/run-aaa/output"
        )

        # The worker-side path must NOT appear in the volumes dict —
        # if it did, the Docker daemon would try to bind a path that
        # doesn't exist outside the worker's mount namespace.
        assert "/app/storage/runs/org-1/run-aaa/input" not in volumes
        assert "/app/storage/runs/org-1/run-aaa/output" not in volumes

        assert expected_host_input in volumes
        assert volumes[expected_host_input]["mode"] == "ro"
        assert expected_host_output in volumes
        assert volumes[expected_host_output]["mode"] == "rw"

    def test_dind_translation_fails_for_path_outside_mount_root(self, tmp_path):
        """A workspace path that doesn't live under the storage mount
        path is a configuration bug — the runner cannot resolve it to
        a host path without the volume mountpoint mapping. Raising
        explicitly is better than silently mounting the wrong dir."""
        mock_docker, mock_client = _make_mock_docker_client()

        outside_path = tmp_path / "outside-mount-root"
        outside_path.mkdir()
        workspace = RunWorkspace(
            run_id="r",
            org_id="o",
            host_input_dir=outside_path / "input",
            host_output_dir=outside_path / "output",
            primary_file=MaterializedFile(
                name="m.idf",
                host_path=outside_path / "input" / "m.idf",
                container_uri=f"file://{CONTAINER_INPUT_DIR}/m.idf",
            ),
        )
        (outside_path / "input").mkdir()
        (outside_path / "output").mkdir()

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner(
                storage_volume="test_storage",
                storage_mount_path="/app/storage",
            )
            runner._client = mock_client

            with pytest.raises(RuntimeError, match="not under storage mount path"):
                runner.run(
                    container_image="test:latest",
                    input_uri=workspace.input_envelope_container_uri,
                    output_uri=workspace.output_envelope_container_uri,
                    workspace=workspace,
                )

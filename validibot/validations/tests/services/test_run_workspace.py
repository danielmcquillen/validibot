"""Tests for the per-run workspace builder.

Covers ADR-2026-04-27 ``[trust-#4]`` Phase 1 Slice 1: the
:class:`RunWorkspaceBuilder` materialises ``runs/<org>/<run>/{input,output}/``
on the host with the right modes, copies the primary submission file and
resource files into ``input/``, and exposes both host paths and
container-visible URIs through the returned :class:`RunWorkspace`.

What we're testing here, and why
--------------------------------

Three concerns each have their own block of tests:

1. **Layout and modes.** Inputs are read-only-ish (mode 755 on dirs, 644 on
   files); output is writable by the container UID. These are the
   mechanical guarantees the runner relies on. A regression here means the
   container either can't write its output (run fails) or the host fails
   to enforce isolation (the very thing this ADR exists to fix).

2. **Path-traversal safety.** Resource filenames flow from workflow
   resource records, which were created by users with workflow-author
   permissions. Even though the resolver now filters access, a hostile
   filename containing ``..`` should never be silently accepted by the
   builder — that would let one workflow's resources land in another
   run's directory. The tests here are the regression fence.

3. **URI generation.** The envelope builder embeds the URIs returned by
   :class:`RunWorkspace`. If the URI helpers drift from the runner's
   mount paths, every advanced run breaks with "file not found." The
   helpers are simple but high-leverage; we lock the strings explicitly.
"""

from __future__ import annotations

import os

import pytest

from validibot.core.storage.local import LocalDataStorage
from validibot.validations.services.run_workspace import CONTAINER_GID
from validibot.validations.services.run_workspace import CONTAINER_INPUT_DIR
from validibot.validations.services.run_workspace import CONTAINER_OUTPUT_DIR
from validibot.validations.services.run_workspace import CONTAINER_UID
from validibot.validations.services.run_workspace import INPUT_DIR_MODE
from validibot.validations.services.run_workspace import INPUT_FILE_MODE
from validibot.validations.services.run_workspace import OUTPUT_DIR_MODE_FALLBACK
from validibot.validations.services.run_workspace import OUTPUT_DIR_MODE_OWNED
from validibot.validations.services.run_workspace import MaterializedFile
from validibot.validations.services.run_workspace import ResourceFileSpec
from validibot.validations.services.run_workspace import RunWorkspace
from validibot.validations.services.run_workspace import RunWorkspaceBuilder
from validibot.validations.services.run_workspace import RunWorkspaceError

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def storage(tmp_path):
    """Local storage rooted under pytest's tmp_path.

    Each test gets a fresh root; we don't share state between tests
    because the workspace builder writes real files.
    """
    return LocalDataStorage(root=tmp_path / "data")


@pytest.fixture
def builder(storage):
    return RunWorkspaceBuilder(storage=storage)


@pytest.fixture
def primary_content():
    """Trivial submission bytes used by most tests."""
    return b'{"hello": "world"}'


# ── Layout and modes ────────────────────────────────────────────────────


class TestLayoutAndModes:
    """The directory skeleton and permissions are the first thing the
    runner relies on. If these guarantees drift, either the container
    can't write its output (run fails noisily) or the host fails to
    enforce read-only inputs (silent leak)."""

    def test_creates_input_and_output_dirs(self, builder, primary_content):
        """input/, input/resources/, and output/ must all exist after build.

        The runner's three-mount strategy depends on these existing —
        Docker won't create the mount source for us, so missing dirs
        manifest as a confusing "no such file or directory" inside the
        container rather than a clean error here.
        """
        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
        )

        assert ws.host_input_dir.is_dir()
        assert (ws.host_input_dir / "resources").is_dir()
        assert ws.host_output_dir.is_dir()

    def test_input_dir_is_mode_755(self, builder, primary_content):
        """Input dirs are 755: readable+executable by everyone, writable
        only by owner. The container reads through this dir but must not
        write into it — that's the read-only invariant the ro mount
        defends."""
        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
        )

        assert ws.host_input_dir.stat().st_mode & 0o777 == INPUT_DIR_MODE, (
            "input/ should be 755"
        )
        assert (
            ws.host_input_dir / "resources"
        ).stat().st_mode & 0o777 == INPUT_DIR_MODE, "input/resources/ should be 755"

    def test_input_files_are_mode_644(self, builder, primary_content, tmp_path):
        """Input files are 644: read-only for everyone except the owner.
        Combined with the read-only mount, this means a buggy validator
        cannot persist a modified copy of its own input back to the
        host."""
        resource_src = tmp_path / "weather.epw"
        resource_src.write_bytes(b"weather data")

        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
            resource_files=[
                ResourceFileSpec(filename="weather.epw", source_path=resource_src),
            ],
        )

        assert ws.primary_file.host_path.stat().st_mode & 0o777 == INPUT_FILE_MODE, (
            "primary submission file should be 644"
        )
        assert (
            ws.resource_files[0].host_path.stat().st_mode & 0o777 == INPUT_FILE_MODE
        ), "resource file should be 644"

    def test_output_dir_is_writable_by_container_uid(self, builder, primary_content):
        """Output dir must be writable by the container — either through
        ``chown`` to UID 1000 + mode 770 (the preferred Kubernetes-style
        path), or through the sticky-world-writable fallback when
        ``chown`` is not permitted (typical on rootless local dev).

        This test accepts either mode because the choice depends on the
        process's privileges. The negative-control test in Slice 4 is
        the one that proves the chosen mode actually works inside a real
        container.
        """
        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
        )

        mode = ws.host_output_dir.stat().st_mode & 0o7777
        assert mode in {
            OUTPUT_DIR_MODE_OWNED,
            OUTPUT_DIR_MODE_FALLBACK,
        }, f"output/ mode should be 770 or 1777, got {oct(mode)}"

    def test_output_dir_is_not_world_writable_when_chown_succeeds(
        self,
        builder,
        primary_content,
        monkeypatch,
    ):
        """When ``chown`` succeeds, the mode must be 770 (not 1777).

        Mainstream Kubernetes practice and the comparative research in
        ``container-sandboxing-comparison.md`` both prefer the owned-770
        pattern over sticky-1777 because it avoids unnecessary
        world-writable bits. We monkeypatch ``os.chown`` to a no-op so
        the test is deterministic regardless of the test runner's
        privileges.
        """

        def fake_chown(path, uid, gid):
            # Pretend chown succeeded; don't actually change ownership.
            return None

        monkeypatch.setattr(os, "chown", fake_chown)

        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
        )

        assert ws.host_output_dir.stat().st_mode & 0o7777 == OUTPUT_DIR_MODE_OWNED

    def test_output_dir_falls_back_to_world_writable_when_chown_denied(
        self,
        builder,
        primary_content,
        monkeypatch,
        caplog,
    ):
        """When ``chown`` raises ``PermissionError``, the builder must
        fall back to mode 1777 and log a warning rather than aborting
        the run. This is the rootless-local-dev path — it is strictly
        less safe than the owned variant, but blocking every local-dev
        run is a worse failure mode than a documented warning.
        """

        def deny_chown(path, uid, gid):
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr(os, "chown", deny_chown)

        with caplog.at_level("WARNING"):
            ws = builder.build(
                org_id="org-1",
                run_id="run-aaa",
                primary_filename="model.idf",
                primary_content=primary_content,
            )

        assert ws.host_output_dir.stat().st_mode & 0o7777 == OUTPUT_DIR_MODE_FALLBACK
        assert any("chown" in r.message for r in caplog.records), (
            "fallback should be logged so operators can investigate"
        )


# ── Materialisation ─────────────────────────────────────────────────────


class TestMaterialization:
    """The builder copies the primary submission file and resource files
    into the workspace. These tests confirm the contents land where the
    container expects them and the URIs match the container path."""

    def test_primary_file_is_written_with_correct_content(
        self, builder, primary_content
    ):
        """The container will read the primary file from
        ``/validibot/input/<original_filename>``. The host bytes must
        match what the dispatch layer passed in, after any preprocessing
        the validator performed."""
        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
        )

        assert ws.primary_file.name == "model.idf"
        assert ws.primary_file.host_path == ws.host_input_dir / "model.idf"
        assert ws.primary_file.host_path.read_bytes() == primary_content

    def test_resource_files_are_copied_to_resources_subdir(
        self, builder, primary_content, tmp_path
    ):
        """Resource files (weather, FMU dependencies, etc.) live under
        ``input/resources/`` so resource names cannot collide with the
        primary filename. The runner can also constrain writes more
        tightly to the resource subtree if needed."""
        weather_src = tmp_path / "USA_CA_SF.epw"
        weather_src.write_bytes(b"epw header\n8760 hours of weather")
        gltf_src = tmp_path / "geometry.gltf"
        gltf_src.write_bytes(b"glTF binary")

        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
            resource_files=[
                ResourceFileSpec(filename="USA_CA_SF.epw", source_path=weather_src),
                ResourceFileSpec(filename="geometry.gltf", source_path=gltf_src),
            ],
        )

        assert len(ws.resource_files) == 2  # noqa: PLR2004 — pinning the count we passed in
        assert ws.resource_files[0].host_path == (
            ws.host_input_dir / "resources" / "USA_CA_SF.epw"
        )
        assert ws.resource_files[0].host_path.read_bytes() == weather_src.read_bytes()
        assert ws.resource_files[1].host_path == (
            ws.host_input_dir / "resources" / "geometry.gltf"
        )

    def test_resource_id_is_passed_through_to_materialized_file(
        self, builder, primary_content, tmp_path
    ):
        """When a spec carries a ``resource_id``, the resulting
        ``MaterializedFile`` carries the same id. The dispatch layer
        relies on this to build the ``resource_uri_overrides`` dict
        keyed by the same identifier the envelope's ``ResourceFileItem.id``
        uses. Without this passthrough the envelope override mechanism
        loses its mapping."""
        src = tmp_path / "weather.epw"
        src.write_bytes(b"data")

        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
            resource_files=[
                ResourceFileSpec(
                    filename="weather.epw",
                    source_path=src,
                    resource_id="vrf-uuid-xyz",
                ),
            ],
        )

        assert ws.resource_files[0].resource_id == "vrf-uuid-xyz"

    def test_resource_id_defaults_to_none_when_not_supplied(
        self, builder, primary_content, tmp_path
    ):
        """The ``resource_id`` field is optional. Specs created without
        one produce materialised files where the field is ``None``,
        preserving compatibility for callers that don't need the
        envelope-override mapping."""
        src = tmp_path / "weather.epw"
        src.write_bytes(b"data")

        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
            resource_files=[
                ResourceFileSpec(filename="weather.epw", source_path=src),
            ],
        )

        assert ws.resource_files[0].resource_id is None

    def test_missing_resource_source_raises_workspace_error(
        self, builder, primary_content, tmp_path
    ):
        """A missing resource source means the workflow is misconfigured
        or the storage backend lost the file. The builder fails the run
        with a workspace-specific error rather than letting it propagate
        as a generic FileNotFoundError, so the dispatch layer can map it
        to a single error category."""
        with pytest.raises(RunWorkspaceError, match="does not exist"):
            builder.build(
                org_id="org-1",
                run_id="run-aaa",
                primary_filename="model.idf",
                primary_content=primary_content,
                resource_files=[
                    ResourceFileSpec(
                        filename="missing.epw",
                        source_path=tmp_path / "nope.epw",
                    ),
                ],
            )

    def test_idempotent_rebuild_overwrites_input(self, builder, primary_content):
        """Re-running the builder for the same run is allowed and
        overwrites input contents. This matters for retries that run
        preprocessing again (e.g. EnergyPlus template resolution might
        produce different bytes on a second attempt with updated
        signals). The output dir is left alone so a previous attempt's
        artifacts remain inspectable for support."""
        ws_first = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=b"version 1",
        )
        # Pretend the previous run wrote some output we don't want to
        # accidentally clobber.
        leftover = ws_first.host_output_dir / "output.json"
        leftover.write_bytes(b"previous run output")

        ws_second = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=b"version 2",
        )

        assert ws_second.primary_file.host_path.read_bytes() == b"version 2"
        # The output dir contents are preserved across rebuilds.
        assert leftover.read_bytes() == b"previous run output"


# ── Path-traversal safety ───────────────────────────────────────────────


class TestPathTraversalSafety:
    """Filenames flow through the workspace builder from user-controlled
    sources (workflow author uploads, original submission filename).
    Each test here is a regression against a specific class of attack;
    they exist because a single missed check could let one run's
    materialisation land in another run's directory, defeating the
    isolation the rest of the ADR is built on."""

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../escape.idf",
            "../../etc/passwd",
            "subdir/inner.idf",
            "/abs/path.idf",
            "./.././.././etc/passwd",
            "..",
            "",
        ],
    )
    def test_rejects_bad_primary_filenames(self, builder, primary_content, bad_name):
        """Anything that isn't a flat filename in the input dir must be
        rejected. We parametrise across the common attack shapes
        (``..``, leading slash, embedded subdir, empty) so the test
        catches a single-pattern hole in the check."""
        with pytest.raises(RunWorkspaceError):
            builder.build(
                org_id="org-1",
                run_id="run-aaa",
                primary_filename=bad_name,
                primary_content=primary_content,
            )

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../weather.epw",
            "../../var/log/weather.epw",
            "subdir/weather.epw",
            "/etc/weather.epw",
            "..",
        ],
    )
    def test_rejects_bad_resource_filenames(
        self, builder, primary_content, tmp_path, bad_name
    ):
        """Resource filenames flow from workflow records, which were
        created by users with workflow-author role. The check applies
        identically to resources because the same class of attack
        applies."""
        # Build a real source file so the rejection happens in the
        # filename validator, not in the missing-source check.
        src = tmp_path / "real-source"
        src.write_bytes(b"data")

        with pytest.raises(RunWorkspaceError, match=r"traversal|Empty"):
            builder.build(
                org_id="org-1",
                run_id="run-aaa",
                primary_filename="model.idf",
                primary_content=primary_content,
                resource_files=[
                    ResourceFileSpec(filename=bad_name, source_path=src),
                ],
            )


# ── URI generation ──────────────────────────────────────────────────────


class TestContainerURIs:
    """The envelope builder embeds the URIs returned by RunWorkspace.
    If the URI helpers drift from the runner's mount paths, every
    advanced run breaks. These tests pin the strings explicitly."""

    def test_input_envelope_uri_uses_container_input_path(
        self, builder, primary_content
    ):
        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
        )
        assert ws.input_envelope_container_uri == (
            f"file://{CONTAINER_INPUT_DIR}/input.json"
        )

    def test_output_envelope_uri_uses_container_output_path(
        self, builder, primary_content
    ):
        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
        )
        assert ws.output_envelope_container_uri == (
            f"file://{CONTAINER_OUTPUT_DIR}/output.json"
        )

    def test_execution_bundle_uri_is_the_output_directory(
        self, builder, primary_content
    ):
        """The validator backend at
        ``validibot-validator-backends/.../energyplus/main.py`` composes
        ``f"{execution_bundle_uri}/outputs"`` for artifacts. Setting the
        bundle URI to the output dir means artifacts land at
        ``/validibot/output/outputs/...`` automatically with no backend
        changes — the entire reason this URI rewriting strategy works
        without coordinated backend releases."""
        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
        )
        assert ws.execution_bundle_container_uri == f"file://{CONTAINER_OUTPUT_DIR}"

    def test_primary_file_container_uri_uses_input_path(self, builder, primary_content):
        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
        )
        assert ws.primary_file.container_uri == (
            f"file://{CONTAINER_INPUT_DIR}/model.idf"
        )

    def test_resource_file_container_uri_uses_resources_subdir(
        self, builder, primary_content, tmp_path
    ):
        weather = tmp_path / "weather.epw"
        weather.write_bytes(b"data")
        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
            resource_files=[
                ResourceFileSpec(filename="weather.epw", source_path=weather),
            ],
        )
        assert ws.resource_files[0].container_uri == (
            f"file://{CONTAINER_INPUT_DIR}/resources/weather.epw"
        )


# ── Integration: paths under DATA_STORAGE_ROOT ──────────────────────────


class TestStorageRootIntegration:
    """The host paths returned by the workspace must live under
    DATA_STORAGE_ROOT and follow the runs/<org>/<run>/ pattern that the
    purge_expired_outputs sweeper expects. A drift here means the
    sweeper silently skips the workspace and disks fill up."""

    def test_workspace_paths_live_under_storage_root(
        self, builder, storage, primary_content
    ):
        ws = builder.build(
            org_id="org-1",
            run_id="run-aaa",
            primary_filename="model.idf",
            primary_content=primary_content,
        )

        expected_base = storage.root / "runs" / "org-1" / "run-aaa"
        assert ws.host_input_dir == expected_base / "input"
        assert ws.host_output_dir == expected_base / "output"
        assert ws.primary_file.host_path == expected_base / "input" / "model.idf"

    def test_workspace_runs_prefix_matches_purge_pattern(
        self, builder, storage, primary_content
    ):
        """The retention sweeper deletes ``runs/<org>/<run>/`` recursively.
        The workspace must live under that exact prefix so retention
        scans it correctly."""
        ws = builder.build(
            org_id="acme-corp",
            run_id="abc-123",
            primary_filename="model.idf",
            primary_content=primary_content,
        )

        # Reconstruct the prefix the sweeper would pass to delete_prefix.
        prefix = "runs/acme-corp/abc-123/"
        resolved_prefix = storage._resolve_path(prefix.rstrip("/"))
        # Both input and output live under that prefix.
        assert resolved_prefix in ws.host_input_dir.parents
        assert resolved_prefix in ws.host_output_dir.parents


# ── Constants smoke-test ────────────────────────────────────────────────


def test_container_uid_and_gid_match_runner_user_setting():
    """The builder owns workspace ownership, the runner owns container
    user. They MUST agree, or the container can't write to its own
    output dir. This test pins the UID/GID values; changing them
    requires coordinated changes in ``runners/docker.py`` (the
    ``user="1000:1000"`` setting). The literal ``1000`` is the contract;
    we deliberately do not abstract it to a constant in the test, since
    the test's job is precisely to detect changes to the constant.
    """
    assert CONTAINER_UID == 1000  # noqa: PLR2004 — pinning the public contract
    assert CONTAINER_GID == 1000  # noqa: PLR2004 — pinning the public contract


def test_container_paths_are_absolute_under_validibot():
    """The fixed container paths form a public contract validator
    backends rely on. Pinned here so any accidental rename surfaces in
    a clear, single test failure rather than a runtime mount error."""
    assert CONTAINER_INPUT_DIR == "/validibot/input"
    assert CONTAINER_OUTPUT_DIR == "/validibot/output"


# ── RunWorkspace dataclass (manual constructor) ─────────────────────────


def test_run_workspace_can_be_constructed_directly(tmp_path):
    """The dataclass should be usable in tests without the builder
    (e.g. when stubbing the workspace for envelope-builder unit tests).
    Uses ``tmp_path`` rather than synthetic ``/tmp/...`` strings so the
    test reads as ordinary, lint-clean Python.
    """
    ws = RunWorkspace(
        run_id="r",
        org_id="o",
        host_input_dir=tmp_path / "in",
        host_output_dir=tmp_path / "out",
        primary_file=MaterializedFile(
            name="m.idf",
            host_path=tmp_path / "in" / "m.idf",
            container_uri="file:///validibot/input/m.idf",
        ),
    )
    assert ws.resource_files == []
    assert ws.input_envelope_container_uri == "file:///validibot/input/input.json"

"""Tests for the ``verify_backup_compatibility`` management command.

The verifier is the gate that decides whether ``just gcp restore``
proceeds.  Wrong answers either way are operator harm:

- **False compatible** → restore proceeds against an incompatible
  schema → silent data corruption → very-bad-day.
- **False incompatible** → operator can't restore from a known-good
  backup during an outage → also very-bad-day.

These tests pin the contract: each refusal class is reported
distinctly, the ``COMPATIBLE`` path requires every check to pass,
and the JSON output is stable for parsing by the just recipe.

Three layers of coverage:

1. **Unit tests on the helper methods** — pure functions, no
   manifest construction needed.
2. **Manifest-driven verdicts** — build a manifest representing
   each refusal class and assert the verdict mentions it.
3. **CLI smoke tests** — exercise ``call_command`` to confirm
   argument parsing + output formatting.
"""

from __future__ import annotations

import json
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from validibot import __version__ as validibot_package_version
from validibot.core.backup_manifest import BACKUP_MANIFEST_SCHEMA_VERSION
from validibot.core.backup_manifest import BackupCompatibility
from validibot.core.backup_manifest import BackupDataComponent
from validibot.core.backup_manifest import BackupFileEntry
from validibot.core.backup_manifest import BackupManifest
from validibot.core.management.commands.verify_backup_compatibility import (
    Command as VerifyCommand,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.django_db

FUTURE_MAJOR_VERSION = 2
REFUSED_EXIT_CODE = 64


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _manifest(
    *,
    validibot_version: str = "0.5.0",
    migration_head: dict[str, str] | None = None,
) -> BackupManifest:
    """Build a manifest with sensible defaults for verifier tests.

    ``migration_head`` defaults to the live test DB's head so the
    "everything matches" case is the default and individual tests
    only have to override the dimension they're testing.
    """
    if migration_head is None:
        migration_head = VerifyCommand()._current_migration_head()  # type: ignore[attr-defined]

    return BackupManifest(
        backup_id="20260504T143022Z",
        created_at="2026-05-04T14:30:22Z",
        target="gcp",
        stage="prod",
        backup_uri="gs://bucket/20260504T143022Z/",
        compatibility=BackupCompatibility(
            validibot_version=validibot_version,
            python_version="3.13.1",
            postgres_server_version="16.0",
            migration_head=migration_head,
        ),
        data=BackupDataComponent(
            db_dump=BackupFileEntry(
                path="db.sql.gz",
                size_bytes=1024,
                checksum_sha256="a" * 64,
            ),
            media_files=[],
        ),
        restore_command_hint="just gcp restore prod gs://bucket/20260504T143022Z/",
    )


# ──────────────────────────────────────────────────────────────────────
# Unit tests on the helper methods
# ──────────────────────────────────────────────────────────────────────


class TestParseMajor:
    """``_parse_major`` returns major-version int or None."""

    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("0.5.0", 0),
            ("1.0.0", 1),
            ("v0.5.0", 0),
            ("v2.3.4", 2),
            ("0.5.0-rc1", 0),
            ("10.20.30", 10),
            # Git SHAs and free-form strings → None (no major).
            ("abc1234", None),
            ("", None),
            ("unknown", None),
        ],
    )
    def test_parses(self, version, expected):
        assert VerifyCommand._parse_major(version) == expected


class TestCurrentRuntimeVersion:
    """The verifier compares backups against the app runtime version."""

    @override_settings(VALIDIBOT_VERSION="1.2.3", VALIDATOR_BACKEND_VERSION="9.9.9")
    def test_prefers_validibot_version(self):
        """The current app version must not be masked by validator-only metadata."""
        assert VerifyCommand()._current_validibot_version() == "1.2.3"

    @override_settings(VALIDIBOT_VERSION="", VALIDATOR_BACKEND_VERSION="9.9.9")
    def test_ignores_validator_backend_version(self):
        """Backend image version is not the app compatibility version."""
        assert VerifyCommand()._current_validibot_version() == validibot_package_version


class TestAppsWhereBackupIsAhead:
    """``_apps_where_backup_is_ahead`` reports apps where backup > current."""

    def test_no_apps_ahead(self):
        backup = {"workflows": "0017_a", "validations": "0046_a"}
        current = {"workflows": "0017_a", "validations": "0046_a"}
        assert VerifyCommand._apps_where_backup_is_ahead(backup, current) == []

    def test_some_apps_ahead(self):
        backup = {"workflows": "0019_new", "validations": "0046_a"}
        current = {"workflows": "0017_a", "validations": "0046_a"}
        assert VerifyCommand._apps_where_backup_is_ahead(backup, current) == [
            "workflows",
        ]

    def test_app_in_backup_but_not_current(self):
        """A backup that has migrations for an app the code doesn't
        know counts as ahead — same failure mode (schema has tables
        the ORM can't model)."""
        backup = {"workflows": "0017", "newapp": "0001"}
        current = {"workflows": "0017"}
        assert VerifyCommand._apps_where_backup_is_ahead(backup, current) == [
            "newapp",
        ]

    def test_current_ahead_of_backup_is_fine(self):
        """A deployment ahead of the backup is the *normal* case
        (operator upgraded between backup and restore).  Restore
        applies the older state and migrations carry it forward."""
        backup = {"workflows": "0015"}
        current = {"workflows": "0017"}
        assert VerifyCommand._apps_where_backup_is_ahead(backup, current) == []

    def test_returns_sorted_for_stable_messages(self):
        """Sorting the output makes operator-facing messages
        deterministic — no flaky test or runbook screenshot."""
        backup = {"zeta": "0002", "alpha": "0002", "mid": "0002"}
        current = {"zeta": "0001", "alpha": "0001", "mid": "0001"}
        assert VerifyCommand._apps_where_backup_is_ahead(backup, current) == [
            "alpha",
            "mid",
            "zeta",
        ]


# ──────────────────────────────────────────────────────────────────────
# Verdict construction
# ──────────────────────────────────────────────────────────────────────


class TestVerdict:
    """End-to-end verdict construction for each refusal class."""

    def test_compatible_when_everything_matches(self):
        cmd = VerifyCommand()
        manifest = _manifest(validibot_version="0.5.0")

        # Stub the current version to match.
        cmd._current_validibot_version = lambda: "0.5.0"  # type: ignore[method-assign]

        verdict = cmd._check(manifest)
        assert verdict["status"] == "COMPATIBLE"
        assert verdict["backup_id"] == "20260504T143022Z"
        assert verdict["current"]["validibot_version"] == "0.5.0"

    def test_refuses_on_migration_head_ahead(self):
        cmd = VerifyCommand()
        # Backup has a migration the deployment doesn't know.
        backup_head = cmd._current_migration_head()
        backup_head["workflows"] = "9999_unknown_future_migration"
        manifest = _manifest(migration_head=backup_head)

        verdict = cmd._check(manifest)
        assert verdict["status"] == "REFUSED"
        codes = [p["code"] for p in verdict["problems"]]
        assert "migration_head" in codes
        problem = next(p for p in verdict["problems"] if p["code"] == "migration_head")
        assert "workflows" in problem["ahead_apps"]

    def test_refuses_on_cross_major_version_jump(self):
        cmd = VerifyCommand()
        manifest = _manifest(validibot_version="2.0.0")
        cmd._current_validibot_version = lambda: "0.5.0"  # type: ignore[method-assign]

        verdict = cmd._check(manifest)
        assert verdict["status"] == "REFUSED"
        codes = [p["code"] for p in verdict["problems"]]
        assert "version_jump" in codes
        problem = next(p for p in verdict["problems"] if p["code"] == "version_jump")
        assert problem["backup_major"] == FUTURE_MAJOR_VERSION
        assert problem["current_major"] == 0

    def test_minor_version_jump_is_compatible(self):
        """Minor version differences (0.4 → 0.5) are routine and allowed.

        AC #16 specifically forbids cross-major; minor / patch
        drift is the normal case — operators upgrade, take
        backups, restore them later under the new code.
        """
        cmd = VerifyCommand()
        manifest = _manifest(validibot_version="0.4.0")
        cmd._current_validibot_version = lambda: "0.5.0"  # type: ignore[method-assign]

        verdict = cmd._check(manifest)
        assert verdict["status"] == "COMPATIBLE"

    def test_unparseable_versions_dont_block(self):
        """Git-SHA-like version strings can't be major-compared.

        We deliberately don't refuse in that case — the migration-
        head check is the stricter gate.  Refusing on
        unparseable versions would block legitimate restores in
        deployments that use SHAs as version stamps.
        """
        cmd = VerifyCommand()
        manifest = _manifest(validibot_version="abc1234")
        cmd._current_validibot_version = lambda: "def5678"  # type: ignore[method-assign]

        verdict = cmd._check(manifest)
        assert verdict["status"] == "COMPATIBLE"

    def test_reports_multiple_problems_at_once(self):
        """An operator hit with one problem expects to see ALL of them.

        Fix-rerun-fix-rerun is operator harm; surface every
        incompatibility in a single verdict.
        """
        cmd = VerifyCommand()
        backup_head = cmd._current_migration_head()
        backup_head["workflows"] = "9999_unknown"
        manifest = _manifest(
            validibot_version="2.0.0",
            migration_head=backup_head,
        )
        cmd._current_validibot_version = lambda: "0.5.0"  # type: ignore[method-assign]

        verdict = cmd._check(manifest)
        assert verdict["status"] == "REFUSED"
        codes = {p["code"] for p in verdict["problems"]}
        assert "migration_head" in codes
        assert "version_jump" in codes


# ──────────────────────────────────────────────────────────────────────
# CLI smoke tests
# ──────────────────────────────────────────────────────────────────────


class TestCli:
    """``call_command`` exercises argument parsing + output formatting."""

    def _write_manifest(self, tmp_path: Path, manifest: BackupManifest) -> Path:
        path = tmp_path / "manifest.json"
        path.write_text(manifest.model_dump_json(), encoding="utf-8")
        return path

    def test_compatible_exits_zero(self, tmp_path: Path):
        manifest = _manifest()
        path = self._write_manifest(tmp_path, manifest)

        out = StringIO()
        # Compatible runs sys.exit(0) — argparse passes that through
        # call_command, but pytest swallows the SystemExit cleanly.
        with pytest.raises(SystemExit) as exc:
            call_command(
                "verify_backup_compatibility",
                "--manifest",
                str(path),
                stdout=out,
            )
        assert exc.value.code == 0
        assert "COMPATIBLE" in out.getvalue()

    def test_refused_exits_64(self, tmp_path: Path):
        cmd = VerifyCommand()
        backup_head = cmd._current_migration_head()
        backup_head["workflows"] = "9999_unknown"
        manifest = _manifest(migration_head=backup_head)
        path = self._write_manifest(tmp_path, manifest)

        out = StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command(
                "verify_backup_compatibility",
                "--manifest",
                str(path),
                stdout=out,
            )
        assert exc.value.code == REFUSED_EXIT_CODE
        assert "REFUSED" in out.getvalue()
        assert "migration_head" in out.getvalue()

    def test_json_output(self, tmp_path: Path):
        """``--json`` emits a machine-parseable verdict.

        The just recipe parses this to surface refusal reasons
        cleanly.  Stable structure here matters more than for the
        human-readable path.
        """
        manifest = _manifest()
        path = self._write_manifest(tmp_path, manifest)

        out = StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command(
                "verify_backup_compatibility",
                "--manifest",
                str(path),
                "--json",
                stdout=out,
            )
        assert exc.value.code == 0
        verdict = json.loads(out.getvalue())
        assert verdict["status"] == "COMPATIBLE"
        assert verdict["backup_id"] == "20260504T143022Z"
        assert "current" in verdict
        assert "backup_compat" in verdict

    def test_missing_manifest_file_raises_command_error(self, tmp_path: Path):
        with pytest.raises(CommandError, match="Manifest not found"):
            call_command(
                "verify_backup_compatibility",
                "--manifest",
                str(tmp_path / "missing.json"),
                stdout=StringIO(),
            )

    def test_malformed_manifest_raises_command_error(self, tmp_path: Path):
        """A manifest that doesn't match the schema fails fast with a
        clear error pointing at the bad field."""
        path = tmp_path / "bad.json"
        path.write_text(
            json.dumps({"backup_id": "missing-other-fields"}),
            encoding="utf-8",
        )

        with pytest.raises(CommandError, match=BACKUP_MANIFEST_SCHEMA_VERSION):
            call_command(
                "verify_backup_compatibility",
                "--manifest",
                str(path),
                stdout=StringIO(),
            )

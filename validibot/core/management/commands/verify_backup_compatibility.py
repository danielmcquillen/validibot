"""Verify a ``BackupManifest`` is restore-compatible with the live deployment.

Run by ``just gcp restore <stage> <backup-uri>`` as the second
pre-flight step (the first being ``doctor --strict``).  The command
fails closed on any of:

1. **Schema-version drift** — manifest reports anything other than
   ``validibot.backup.v1``.  Future schemas need an explicit
   migration path; fail-closed prevents partial-restore disasters.
2. **Migration-head ahead of code** — the backup applied migrations
   the current deployment doesn't know about.  Importing would mean
   the schema has tables / columns the ORM can't model, which
   cascades into runtime errors and possible data loss.  Operators
   must upgrade the deployment first, then restore.
3. **Cross-major Validibot version jump** — boring self-hosting
   ADR AC #16 forbids cross-major-version restores.  Operators
   route through documented intermediate releases.

Compatible runs exit 0 with a one-line ``COMPATIBLE`` message.
Incompatible runs exit non-zero with a structured ``REFUSED``
report — the just recipe parses this to surface the reason cleanly
to the operator.

Why this is a separate command from ``write_backup_manifest``
=============================================================

Write happens once; verify happens repeatedly (operators inspect
backups before deciding to restore).  Splitting the command means
verify runs in milliseconds against any manifest the operator has
on hand, not just the one freshly produced.  The verify side also
needs no GCS write permissions, only read — operators can run it
from their laptop against a downloaded manifest if they want a
quick "would this be restorable?" check.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from pydantic import ValidationError

from validibot.core.backup_manifest import BACKUP_MANIFEST_SCHEMA_VERSION
from validibot.core.backup_manifest import BackupManifest
from validibot.core.deployment import get_validibot_runtime_version

# Exit codes — operators / CI can branch on these.  Plain non-zero
# would require parsing stderr, which is fragile.
EXIT_COMPATIBLE = 0
EXIT_REFUSED = 64
EXIT_USAGE = 65


class Command(BaseCommand):
    """Verify a ``BackupManifest`` is restore-compatible with this deployment.

    Usage::

        python manage.py verify_backup_compatibility \\
            --manifest gs://bucket/20260504T143022Z/manifest.json

    The ``--manifest`` flag accepts ``gs://``, a local file path,
    or ``-`` for stdin (useful for piped pre-flight checks).

    Output (compatible)::

        COMPATIBLE: backup 20260504T143022Z restorable on deployment.
          backup_version: abc1234   current_version: abc1234
          backup_pg:      16.0      current_pg:      16.0
          backup_apps:    12        current_apps:    12

    Output (refused) — exit 64, prefixed REFUSED, lists every
    incompatibility found::

        REFUSED: backup is incompatible with this deployment.
          schema_version: backup is validibot.backup.v2 (this code reads v1)
          migration_head: backup ahead of code in apps: workflows
          version_jump:   backup 0.6.x but deployment is 0.4.x (cross-major)
    """

    help = "Verify a BackupManifest is restore-compatible with this deployment."

    def add_arguments(self, parser):
        parser.add_argument(
            "--manifest",
            required=True,
            help=(
                "Path to manifest.json. Accepts gs:// URIs, local "
                "filesystem paths, or '-' for stdin."
            ),
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help=(
                "Emit a JSON object instead of human-readable text. "
                "Useful for pipelines that want to machine-parse the "
                "verification result."
            ),
        )

    def handle(self, *args, **options):
        manifest = self._load_manifest(options["manifest"])
        verdict = self._check(manifest)

        if options["json"]:
            self.stdout.write(json.dumps(verdict, indent=2))
        else:
            self._print_human(verdict)

        if verdict["status"] == "COMPATIBLE":
            sys.exit(EXIT_COMPATIBLE)
        sys.exit(EXIT_REFUSED)

    # ── Manifest loading ────────────────────────────────────────────────

    def _load_manifest(self, source: str) -> BackupManifest:
        """Load a manifest from gs://, a local file, or stdin.

        We parse via ``BackupManifest.model_validate_json`` rather
        than reading the dict directly so a malformed manifest
        produces a clear ValidationError pointing at the bad field
        — much nicer to debug than a ``KeyError`` 50 lines into
        ``_check``.  We also catch the ValidationError here and
        re-raise as ``CommandError`` so the operator sees a clean
        error rather than a Python traceback.
        """
        try:
            payload = self._read_source(source)
        except FileNotFoundError as exc:
            msg = f"Manifest not found: {source}"
            raise CommandError(msg) from exc

        try:
            return BackupManifest.model_validate_json(payload)
        except ValidationError as exc:
            # Pydantic's error report is verbose but precise; pass
            # it through so operators can see exactly which field
            # failed.  CommandError exits with the standard error
            # code, distinct from EXIT_REFUSED.
            msg = f"Manifest is not valid {BACKUP_MANIFEST_SCHEMA_VERSION}:\n{exc}"
            raise CommandError(msg) from exc

    def _read_source(self, source: str) -> str:
        """Return the raw text of the manifest from the given source."""
        if source == "-":
            return sys.stdin.read()
        if source.startswith("gs://"):
            return self._read_from_gcs(source)
        return Path(source).read_text(encoding="utf-8")

    def _read_from_gcs(self, uri: str) -> str:
        """Read a gs:// URI via google-cloud-storage."""
        try:
            from google.cloud import storage
        except ImportError as exc:
            msg = (
                "Reading the manifest from a gs:// URI requires the "
                "google-cloud-storage package. Pass --manifest as a "
                "local path if running outside the GCP runtime."
            )
            raise CommandError(msg) from exc

        without_scheme = uri[len("gs://") :]
        bucket_name, _, object_name = without_scheme.partition("/")
        if not bucket_name or not object_name:
            msg = f"Invalid gs:// URI: {uri!r}"
            raise CommandError(msg)

        client = storage.Client()
        blob = client.bucket(bucket_name).blob(object_name)
        return blob.download_as_text(encoding="utf-8")

    # ── Compatibility checks ────────────────────────────────────────────

    def _check(self, manifest: BackupManifest) -> dict:
        """Run every compatibility check; return a structured verdict.

        Multiple incompatibilities are all reported, not just the
        first.  An operator who got slapped with "ERR1" expects to
        see "ERR2 + ERR3" too rather than fix-rerun-fix-rerun.
        """
        problems: list[dict] = []

        # 1. Schema version.  Literal[v1] in the model means we
        # never reach this branch in practice (parsing fails
        # earlier), but the check is here for symmetry: a schema-
        # version-only mismatch is a distinct failure mode worth
        # explicit reporting.
        if manifest.schema_version != BACKUP_MANIFEST_SCHEMA_VERSION:
            problems.append(
                {
                    "code": "schema_version",
                    "message": (
                        f"backup is {manifest.schema_version} "
                        f"(this code reads {BACKUP_MANIFEST_SCHEMA_VERSION})"
                    ),
                },
            )

        # 2. Migration head.  We compare per-app: any app where
        # the backup's most recent migration is greater than what
        # this code knows about means the backup applied
        # something we can't reproduce.
        current_head = self._current_migration_head()
        ahead = self._apps_where_backup_is_ahead(
            manifest.compatibility.migration_head,
            current_head,
        )
        if ahead:
            problems.append(
                {
                    "code": "migration_head",
                    "message": f"backup ahead of code in apps: {', '.join(ahead)}",
                    "ahead_apps": ahead,
                },
            )

        # 3. Cross-major version mismatch.  If we can parse both
        # versions as semver (or at least extract a major
        # component), refuse ANY major mismatch — both directions.
        #
        # Why both directions:
        #
        #   - backup_major > current_major (backup is newer):
        #       Restoring would require migrations the current code
        #       doesn't ship; the database schema would be ahead of
        #       the ORM. Already caught by the migration_head check
        #       above, but we surface it here too with a clearer
        #       message about the version axis.
        #
        #   - backup_major < current_major (backup is older):
        #       Earlier we permitted this, on the theory that
        #       ``migrate`` would forward-port the older state. That
        #       bypasses the ADR's controlled cross-major upgrade
        #       path: an operator on v1.x could restore a v0 backup
        #       and let migrations run, skipping the deliberate
        #       v0 → v0.last_minor → v1.0 step that the upgrade
        #       recipe enforces. Boring-self-hosting AC #16: cross-
        #       major restores must route through documented
        #       intermediate releases.
        #
        # If parsing fails (custom version strings), we permit the
        # restore — the operator's existing version-pinning discipline
        # is the gate, and we don't want to refuse purely because
        # we couldn't parse a freeform string.
        backup_major = self._parse_major(manifest.compatibility.validibot_version)
        current_major = self._parse_major(self._current_validibot_version())
        if (
            backup_major is not None
            and current_major is not None
            and backup_major != current_major
        ):
            if backup_major > current_major:
                detail = (
                    "backup is from a newer major version — upgrade the "
                    "deployment first, then restore."
                )
            else:
                detail = (
                    "backup is from an older major version — restore via "
                    "the documented upgrade path (boring-self-hosting "
                    "ADR AC #16) instead of directly onto current code."
                )
            problems.append(
                {
                    "code": "version_jump",
                    "message": (
                        f"backup is major v{backup_major} "
                        f"but deployment is major v{current_major}. "
                        f"{detail}"
                    ),
                    "backup_major": backup_major,
                    "current_major": current_major,
                },
            )

        if problems:
            return {
                "status": "REFUSED",
                "backup_id": manifest.backup_id,
                "problems": problems,
                "backup_compat": manifest.compatibility.model_dump(),
                "current": {
                    "validibot_version": self._current_validibot_version(),
                    "migration_head": current_head,
                },
            }

        return {
            "status": "COMPATIBLE",
            "backup_id": manifest.backup_id,
            "backup_compat": manifest.compatibility.model_dump(),
            "current": {
                "validibot_version": self._current_validibot_version(),
                "migration_head": current_head,
            },
        }

    # ── Live-state queries ──────────────────────────────────────────────

    def _current_migration_head(self) -> dict[str, str]:
        """Same logic as ``write_backup_manifest`` — kept inline rather than
        factored into a shared helper because the two commands have
        different lifecycles and shared helpers tend to grow features
        the wrong direction."""
        recorder = MigrationRecorder(connection)
        applied = recorder.applied_migrations()
        head: dict[str, str] = {}
        for app_label, migration_name in applied:
            current = head.get(app_label, "")
            if migration_name > current:
                head[app_label] = migration_name
        return head

    def _current_validibot_version(self) -> str:
        return get_validibot_runtime_version()

    @staticmethod
    def _apps_where_backup_is_ahead(
        backup_head: dict[str, str],
        current_head: dict[str, str],
    ) -> list[str]:
        """Return app labels where the backup's migration head is
        ahead of the current deployment's.

        "Ahead" means: the backup's migration-name string is
        lexicographically greater than the current's, OR the
        backup has migrations for an app that current doesn't
        know at all.  Migration names start with a 4-digit
        prefix, so string compare matches numeric ordering.
        """
        ahead: list[str] = []
        for app_label, backup_migration in backup_head.items():
            current_migration = current_head.get(app_label, "")
            if backup_migration > current_migration:
                ahead.append(app_label)
        return sorted(ahead)

    @staticmethod
    def _parse_major(version: str) -> int | None:
        """Extract the major version digit from a version string.

        Accepts ``0.4.0``, ``0.4.0-rc1``, ``v0.4.0``, ``abc1234``
        (git SHA, returns None — no major to compare), etc.  We're
        deliberately permissive: a parse failure means "we don't
        know, don't refuse on this dimension."  The migration-head
        check is the stricter gate; this is a coarser-grained
        backstop for cross-release-cycle drift.
        """
        match = re.match(r"^v?(\d+)\.(\d+)", version or "")
        if not match:
            return None
        return int(match.group(1))

    # ── Output ──────────────────────────────────────────────────────────

    def _print_human(self, verdict: dict) -> None:
        if verdict["status"] == "COMPATIBLE":
            self.stdout.write(
                self.style.SUCCESS(
                    f"COMPATIBLE: backup {verdict['backup_id']} "
                    "restorable on deployment.",
                ),
            )
        else:
            self.stdout.write(
                self.style.ERROR(
                    "REFUSED: backup is incompatible with this deployment.",
                ),
            )
            for problem in verdict["problems"]:
                self.stdout.write(f"  {problem['code']}: {problem['message']}")

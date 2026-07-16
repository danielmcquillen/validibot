from django.db import migrations
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase


class ValidationMigrationGraphTests(SimpleTestCase):
    """Regression tests for the validation migration graph.

    The graph retains the stable pre-Step-I/O prefix through migration 0025,
    then applies one intentionally destructive current-schema tail. These
    checks keep that cut line explicit and prevent accidental missing links.
    """

    def test_agent_access_migration_keeps_original_0023_dependency(self) -> None:
        """The retained prefix must remain internally connected through 0024."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[("validations", "0024_agent_access_fields")]

        self.assertIn(
            ("validations", "0023_add_evidence_hash_to_validation_run"),
            migration.dependencies,
        )

    def test_current_schema_replaces_legacy_evidence_hash(self) -> None:
        """A fresh cutover must drop ``evidence_hash`` and add ``output_hash``.

        No data copy is intentional: supported deployments rebuild from an
        empty database, so preserving the obsolete column would misrepresent
        the current evidence contract and imply compatibility we do not offer.
        """
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[("validations", "0026_current_schema")]

        self.assertIn(
            ("validations", "0025_fmu_allow_custom_assertion_targets"),
            migration.dependencies,
        )
        removed_fields = {
            (operation.model_name, operation.name)
            for operation in migration.operations
            if isinstance(operation, migrations.RemoveField)
        }
        added_fields = {
            (operation.model_name, operation.name)
            for operation in migration.operations
            if isinstance(operation, migrations.AddField)
        }

        self.assertIn(("validationrun", "evidence_hash"), removed_fields)
        self.assertIn(("validationrun", "output_hash"), added_fields)

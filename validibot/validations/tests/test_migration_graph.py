from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase


class ValidationMigrationGraphTests(TestCase):
    """Regression tests for the validation migration graph.

    These checks preserve compatibility with deployed databases that already
    recorded ``0023_add_evidence_hash_to_validation_run`` before the field was
    renamed to ``output_hash`` in application code.
    """

    def test_agent_access_migration_keeps_original_0023_dependency(self) -> None:
        """``0024_agent_access_fields`` must still depend on the applied 0023."""
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        migration = loader.disk_migrations[("validations", "0024_agent_access_fields")]

        self.assertIn(
            ("validations", "0023_add_evidence_hash_to_validation_run"),
            migration.dependencies,
        )

    def test_output_hash_is_a_forward_rename_migration(self) -> None:
        """``output_hash`` must arrive via a new tail migration."""
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        migration = loader.disk_migrations[
            ("validations", "0036_rename_evidence_hash_to_output_hash")
        ]

        self.assertEqual(
            migration.dependencies,
            [("validations", "0035_normalize_library_signal_bindings")],
        )
        self.assertEqual(
            [type(operation).__name__ for operation in migration.operations],
            ["RenameField", "AlterField"],
        )

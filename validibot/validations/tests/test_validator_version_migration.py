"""Tests for collapsing legacy validator version labels to integers.

The live ``Validator.version`` field is now a positive integer revision. These
tests pin the migration helper that rewrites old labels such as ``"1.2"`` and
``""`` without deleting validator rows. That row-identity guarantee matters
because workflow steps point at concrete validator FKs.
"""

import importlib

migration = importlib.import_module(
    "validibot.validations.migrations.0056_collapse_validator_versions_to_integers",
)


def test_semantic_revision_labels_map_to_integer_revisions():
    """Historic ``1.x`` catalog labels become the corresponding revision."""
    rows = [
        migration.VersionRow(pk=1, slug="energyplus", version="1.0"),
        migration.VersionRow(pk=2, slug="energyplus", version="1.1"),
        migration.VersionRow(pk=3, slug="energyplus", version="1.2"),
    ]

    rewrites = migration._build_integer_rewrites(rows)

    assert rewrites == {1: 1, 2: 2, 3: 3}


def test_blank_legacy_label_sorts_before_declared_semantic_revision():
    """A stale blank row should not become newer than the declared config row."""
    rows = [
        migration.VersionRow(pk=1, slug="shacl", version=""),
        migration.VersionRow(pk=2, slug="shacl", version="0.3"),
    ]

    rewrites = migration._build_integer_rewrites(rows)

    assert rewrites == {1: 1, 2: 3}


def test_collision_fallback_preserves_unique_positive_integers():
    """Labels that collapse to the same integer are assigned free revisions."""
    rows = [
        migration.VersionRow(pk=1, slug="basic", version="1"),
        migration.VersionRow(pk=2, slug="basic", version="1.0"),
        migration.VersionRow(pk=3, slug="basic", version="01"),
    ]

    rewrites = migration._build_integer_rewrites(rows)

    assert rewrites == {2: 2, 3: 3}

"""Collapse legacy validator version labels to integer strings.

``Validator.version`` is moving from a free-form string label to a positive
integer column. Earlier rows used labels such as ``"1.0"``, ``"1.2"``,
``"0.3"``, or ``""``. This migration rewrites every validator family to
unique positive integer labels before the schema migration casts the column.

The important safety property is row identity: workflow steps already point at
specific ``Validator`` rows, so the migration must update the version value in
place without deleting or recreating rows.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import migrations

if TYPE_CHECKING:
    from collections.abc import Iterable


SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$",
)
PARTIAL_SEMVER_PATTERN = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)$")


@dataclass(frozen=True)
class VersionRow:
    """Small value object for deterministic family-level rewrites."""

    pk: int
    slug: str
    version: str


def _canonical_positive_integer(value: str) -> int | None:
    """Return a positive integer only when ``value`` is already canonical."""
    if not value.isdigit():
        return None
    parsed = int(value)
    if parsed < 1:
        return None
    if value != str(parsed):
        return None
    return parsed


def _preferred_legacy_integer(value: str) -> int | None:
    """Map common historic semantic labels to compact integer revisions.

    Validibot's system validator configs historically used ``0.x`` and ``1.x``
    labels as revision counters. Mapping ``1.2`` to ``3`` and ``0.3`` to ``3``
    preserves their position in that history even on databases that only have
    the latest row. Arbitrary labels fall through to family-local allocation.
    """
    match = SEMVER_PATTERN.match(value)
    if match and match.group("major") == "0":
        return max(int(match.group("minor")), 1)
    if match and match.group("major") == "1":
        return int(match.group("minor")) + 1
    match = PARTIAL_SEMVER_PATTERN.match(value)
    if match and match.group("major") == "0":
        return max(int(match.group("minor")), 1)
    if match and match.group("major") == "1":
        return int(match.group("minor")) + 1
    return _canonical_positive_integer(value)


def _legacy_sort_key(row: VersionRow) -> tuple[int, int, int, int, str, int]:
    """Sort labels in old-to-new order for collision fallback allocation."""
    version = row.version
    if version == "":
        return (0, 0, 0, 0, version, row.pk)
    match = SEMVER_PATTERN.match(version)
    if match:
        return (
            1,
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            version,
            row.pk,
        )
    match = PARTIAL_SEMVER_PATTERN.match(version)
    if match:
        return (
            2,
            int(match.group("major")),
            int(match.group("minor")),
            0,
            version,
            row.pk,
        )
    if version.isdigit():
        return (3, int(version), 0, 0, version, row.pk)
    return (4, 0, 0, 0, version, row.pk)


def _build_integer_rewrites(rows: Iterable[VersionRow]) -> dict[int, int]:
    """Return ``{validator_pk: new_version}`` for rows needing normalization."""
    families: dict[str, list[VersionRow]] = defaultdict(list)
    for row in rows:
        families[row.slug].append(row)

    rewrites: dict[int, int] = {}
    for family_rows in families.values():
        used: set[int] = set()
        noncanonical: list[VersionRow] = []

        # Existing canonical integer revisions already mean exactly what the
        # new field means. Reserve those first so a legacy label like "1.0"
        # cannot steal "1" from a row that is already labelled "1".
        for row in sorted(family_rows, key=_legacy_sort_key):
            canonical = _canonical_positive_integer(row.version)
            if canonical is None or canonical in used:
                noncanonical.append(row)
                continue
            used.add(canonical)

        pending: list[VersionRow] = []

        for row in sorted(noncanonical, key=_legacy_sort_key):
            preferred = _preferred_legacy_integer(row.version)
            if preferred is None or preferred in used:
                pending.append(row)
                continue
            used.add(preferred)
            if row.version != str(preferred):
                rewrites[row.pk] = preferred

        next_candidate = 1
        for row in pending:
            while next_candidate in used:
                next_candidate += 1
            rewrites[row.pk] = next_candidate
            used.add(next_candidate)
            next_candidate += 1

    return rewrites


def collapse_versions_to_integer_strings(apps, schema_editor):
    """Rewrite every validator family so all labels are positive integers."""
    validator_model = apps.get_model("validations", "Validator")

    rows = [
        VersionRow(
            pk=row["id"],
            slug=row["slug"],
            version=str(row["version"] or "").strip(),
        )
        for row in validator_model.objects.all().values("id", "slug", "version")
    ]
    rewrites = _build_integer_rewrites(rows)

    if not rewrites:
        return

    for pk in rewrites:
        validator_model.objects.filter(pk=pk).update(
            version=f"__tmp_int_version_{pk}",
        )
    for pk, version in rewrites.items():
        validator_model.objects.filter(pk=pk).update(version=str(version))


class Migration(migrations.Migration):
    dependencies = [
        ("validations", "0055_alter_workflowstepiopromotion_signal_definition_and_more"),
    ]

    operations = [
        migrations.RunPython(
            collapse_versions_to_integer_strings,
            migrations.RunPython.noop,
        ),
    ]

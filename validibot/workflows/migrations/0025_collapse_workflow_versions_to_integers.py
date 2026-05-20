"""Collapse legacy workflow version labels to integer strings.

``Workflow.version`` is moving from a string label to a positive integer
column. Earlier schema versions allowed strict semver labels and, for a time,
empty labels. This migration normalizes every workflow family to unique
positive integer labels before the column type changes in the next migration.

The important safety property is collision avoidance. A family may contain
both ``"1"`` and ``"1.0.0"`` because those are distinct strings under the old
unique constraint; both would become ``1`` if the database cast them directly
to integers. We therefore preserve canonical integer labels and assign every
non-canonical label the first free positive integer in that ``(org, slug)``
family. Rows that need rewriting are first moved to unique temporary labels so
the existing ``(org, slug, version)`` unique constraint cannot be tripped by
an intermediate update.
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
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)
PARTIAL_SEMVER_PATTERN = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)$")


@dataclass(frozen=True)
class VersionRow:
    """Small value object for deterministic family-level rewrites."""

    pk: int
    org_id: int
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


def _legacy_sort_key(row: VersionRow) -> tuple[int, int, int, int, str, int]:
    """Sort legacy non-integer labels in a stable, human-readable order."""
    version = row.version
    match = SEMVER_PATTERN.match(version)
    if match:
        return (
            0,
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            version,
            row.pk,
        )
    match = PARTIAL_SEMVER_PATTERN.match(version)
    if match:
        return (
            1,
            int(match.group("major")),
            int(match.group("minor")),
            0,
            version,
            row.pk,
        )
    if version.isdigit():
        return (2, int(version), 0, 0, version, row.pk)
    return (3, 0, 0, 0, version, row.pk)


def _build_integer_rewrites(rows: Iterable[VersionRow]) -> dict[int, int]:
    """Return ``{workflow_pk: new_version}`` for rows needing normalization."""
    families: dict[tuple[int, str], list[VersionRow]] = defaultdict(list)
    for row in rows:
        families[(row.org_id, row.slug)].append(row)

    rewrites: dict[int, int] = {}
    for family_rows in families.values():
        used = {
            parsed
            for parsed in (
                _canonical_positive_integer(row.version) for row in family_rows
            )
            if parsed is not None
        }
        next_candidate = 1
        noncanonical_rows = [
            row
            for row in family_rows
            if _canonical_positive_integer(row.version) is None
        ]
        for row in sorted(noncanonical_rows, key=_legacy_sort_key):
            while next_candidate in used:
                next_candidate += 1
            rewrites[row.pk] = next_candidate
            used.add(next_candidate)
            next_candidate += 1
    return rewrites


def collapse_versions_to_integer_strings(apps, schema_editor):
    """Rewrite every family so all labels are unique positive integers."""
    workflow_model = apps.get_model("workflows", "Workflow")

    rows = [
        VersionRow(
            pk=row["id"],
            org_id=row["org_id"],
            slug=row["slug"],
            version=str(row["version"] or "").strip(),
        )
        for row in workflow_model.objects.all().values(
            "id",
            "org_id",
            "slug",
            "version",
        )
    ]
    rewrites = _build_integer_rewrites(rows)

    if not rewrites:
        return

    for pk in rewrites:
        workflow_model.objects.filter(pk=pk).update(
            version=f"__tmp_int_version_{pk}",
        )
    for pk, version in rewrites.items():
        workflow_model.objects.filter(pk=pk).update(version=str(version))


class Migration(migrations.Migration):
    dependencies = [
        ("workflows", "0024_alter_workflow_version_required"),
    ]

    operations = [
        migrations.RunPython(
            collapse_versions_to_integer_strings,
            migrations.RunPython.noop,
        ),
    ]

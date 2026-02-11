"""
Utilities for workflow version resolution and comparison.

This module provides version parsing, comparison, and resolution logic
for the org-scoped API (ADR-2026-01-06).
"""

from __future__ import annotations

import re
from functools import total_ordering
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from validibot.workflows.models import Workflow

# Pattern for validating/parsing semantic versions
SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)(?:\.(?P<minor>0|[1-9]\d*)(?:\.(?P<patch>0|[1-9]\d*))?)?$"
)


@total_ordering
class ParsedVersion:
    """
    Parsed version for comparison.

    Normalizes integer versions (e.g., "1") to semver (1.0.0) for comparison.
    This allows consistent ordering of mixed version formats.

    Examples:
        >>> ParsedVersion("1") < ParsedVersion("2")
        True
        >>> ParsedVersion("1") == ParsedVersion("1.0.0")
        True
        >>> ParsedVersion("1.2") < ParsedVersion("1.10")
        True
    """

    def __init__(self, version_str: str):
        self.original = version_str
        self.major = 0
        self.minor = 0
        self.patch = 0
        self._parse(version_str)

    def _parse(self, version_str: str) -> None:
        if not version_str:
            return

        # Handle simple integer versions
        if version_str.isdigit():
            self.major = int(version_str)
            return

        # Handle semantic versions
        match = SEMVER_PATTERN.match(version_str)
        if match:
            self.major = int(match.group("major"))
            self.minor = int(match.group("minor") or 0)
            self.patch = int(match.group("patch") or 0)

    def as_tuple(self) -> tuple[int, int, int]:
        """Return version as (major, minor, patch) tuple."""
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ParsedVersion):
            return NotImplemented
        return self.as_tuple() == other.as_tuple()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ParsedVersion):
            return NotImplemented
        return self.as_tuple() < other.as_tuple()

    def __hash__(self) -> int:
        return hash(self.as_tuple())

    def __repr__(self) -> str:
        return f"ParsedVersion({self.original!r})"

    def __str__(self) -> str:
        return self.original


def get_latest_workflow(
    queryset: QuerySet[Workflow],
    *,
    include_archived: bool = False,
) -> Workflow | None:
    """
    Get the latest workflow from a queryset of workflows with the same slug.

    "Latest" is determined by:
    1. Non-archived (unless include_archived=True)
    2. Highest version (normalized: "1" treated as 1.0.0)
    3. Most recent created timestamp as tiebreaker

    Args:
        queryset: Queryset of workflows (typically filtered by org + slug)
        include_archived: If True, include archived workflows in the result

    Returns:
        The latest workflow, or None if the queryset is empty
    """
    if not include_archived:
        queryset = queryset.filter(is_archived=False)

    workflows = list(queryset)
    if not workflows:
        return None

    # Sort by version descending, then created descending (as tiebreaker)
    def sort_key(wf: Workflow) -> tuple[tuple[int, int, int], float]:
        pv = ParsedVersion(wf.version)
        # Negate for descending order
        return (
            (-pv.major, -pv.minor, -pv.patch),
            -wf.created.timestamp(),
        )

    workflows.sort(key=sort_key)
    return workflows[0]


def get_latest_workflow_ids(
    queryset: QuerySet[Workflow],
) -> list[int]:
    """
    Get the IDs of the latest version of each workflow family in the queryset.

    A workflow "family" is defined by (org, slug). This function returns
    the ID of the latest version for each unique family.

    Uses O(n) time complexity by grouping in Python after a single DB query.

    Args:
        queryset: Queryset of workflows

    Returns:
        List of workflow IDs representing the latest version of each family
    """
    from collections import defaultdict

    # Fetch all non-archived workflows in a single query
    workflows = list(
        queryset.filter(is_archived=False).only(
            "id",
            "org_id",
            "slug",
            "version",
            "created",
        )
    )

    if not workflows:
        return []

    # Group by (org_id, slug) family
    families: dict[tuple[int, str], list] = defaultdict(list)
    for wf in workflows:
        families[(wf.org_id, wf.slug)].append(wf)

    # Find the latest in each family using version comparison
    latest_ids = []
    for family_workflows in families.values():
        # Sort by version descending, then created descending (as tiebreaker)
        def sort_key(wf) -> tuple[tuple[int, int, int], float]:
            pv = ParsedVersion(wf.version)
            return ((-pv.major, -pv.minor, -pv.patch), -wf.created.timestamp())

        family_workflows.sort(key=sort_key)
        latest_ids.append(family_workflows[0].pk)

    return latest_ids

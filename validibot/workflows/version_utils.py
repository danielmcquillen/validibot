"""
Utilities for workflow version resolution and comparison.

Workflow versions are positive integers. They are ordering keys for rows in a
workflow family, not semantic-release labels.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from validibot.workflows.models import Workflow


def parse_workflow_version(value: int | str) -> int:
    """Return ``value`` as a positive integer workflow version.

    The database column is an integer, but this helper intentionally accepts
    strings because forms, fixtures, and tests often pass request-shaped data.
    Invalid data raises loudly so bad historical rows cannot be sorted as a
    real workflow version.
    """
    if value is None or value == "":
        raise ValueError("Workflow version is required.")
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Workflow version must be a positive integer.") from exc
    if version < 1:
        raise ValueError("Workflow version must be a positive integer.")
    return version


def compare_workflow_versions(left: int | str, right: int | str) -> int:
    """Compare two workflow version labels.

    Returns ``-1`` when ``left < right``, ``0`` when they are equal, and
    ``1`` when ``left > right``. Accepted labels are positive integers.
    """
    left_version = parse_workflow_version(left)
    right_version = parse_workflow_version(right)
    if left_version < right_version:
        return -1
    if left_version > right_version:
        return 1
    return 0


def get_latest_workflow(
    queryset: QuerySet[Workflow],
    *,
    include_archived: bool = False,
) -> Workflow | None:
    """
    Get the latest workflow from a queryset of workflows with the same slug.

    "Latest" is determined by:
    1. Non-archived (unless include_archived=True)
    2. Highest integer version
    3. Most recent created timestamp as tiebreaker

    Args:
        queryset: Queryset of workflows (typically filtered by org + slug)
        include_archived: If True, include archived workflows in the result

    Returns:
        The latest workflow, or None if the queryset is empty
    """
    if not include_archived:
        queryset = queryset.filter(is_archived=False, is_tombstoned=False)

    workflows = list(queryset)
    if not workflows:
        return None

    # Sort by version descending, then created descending (as tiebreaker)
    def sort_key(wf: Workflow) -> tuple[int, float]:
        return (-parse_workflow_version(wf.version), -wf.created.timestamp())

    workflows.sort(key=sort_key)
    return workflows[0]


def get_latest_workflow_ids(
    queryset: QuerySet[Workflow],
    *,
    include_archived: bool = False,
) -> list[int]:
    """
    Get the IDs of the latest version of each workflow family in the queryset.

    A workflow "family" is defined by (org, slug). This function returns
    the ID of the latest version for each unique family.

    Uses O(n) time complexity by grouping in Python after a single DB query.

    Args:
        queryset: Queryset of workflows
        include_archived: If True, archived rows can win latest-version
            selection. This lets callers hide an entire family when its newest
            row is archived, instead of showing an older active row as though
            it were current.

    Returns:
        List of workflow IDs representing the latest version of each family
    """
    from collections import defaultdict

    queryset = queryset.filter(is_tombstoned=False)
    if not include_archived:
        queryset = queryset.filter(is_archived=False)

    # This helper only needs scalar fields for version comparison. Callers
    # may pass a display queryset with select_related() already attached; clear
    # it before using only() so Django does not see a relation as both traversed
    # and deferred.
    workflows = list(
        queryset.select_related(None)
        .prefetch_related(None)
        .only(
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
        def sort_key(wf) -> tuple[int, float]:
            return (-parse_workflow_version(wf.version), -wf.created.timestamp())

        family_workflows.sort(key=sort_key)
        latest_ids.append(family_workflows[0].pk)

    return latest_ids

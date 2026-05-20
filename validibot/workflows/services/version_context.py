"""Workflow version-family helpers for UI and API surfaces.

Workflow versions are separate rows that share ``(org, slug)``. Views should
present that family consistently: newest version first, the current row marked,
and archived or inactive rows labelled without hiding the fact that they are
real historical definitions.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.core.utils import reverse_with_org
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.models import Workflow
from validibot.workflows.version_utils import get_latest_workflow
from validibot.workflows.version_utils import parse_workflow_version

if TYPE_CHECKING:
    from django.http import HttpRequest


def get_visible_workflow_family(*, workflow: Workflow, user) -> list[Workflow]:
    """Return visible sibling versions for ``workflow`` ordered newest first."""

    if getattr(user, "is_authenticated", False) and getattr(
        user,
        "is_superuser",
        False,
    ):
        queryset = Workflow.objects.filter(org=workflow.org, slug=workflow.slug)
    else:
        queryset = Workflow.objects.for_user(user).filter(
            org=workflow.org,
            slug=workflow.slug,
        )
    versions = list(queryset.select_related("org").order_by("created"))
    versions.sort(key=_version_sort_key, reverse=True)
    return versions


def build_workflow_version_context(
    *,
    request: HttpRequest,
    workflow: Workflow,
) -> dict[str, Any]:
    """Build template context for workflow version history.

    Returns one context dict that drives the workflow detail history card:

    - ``workflow_versions``: a compact list-of-dicts feeding the
      ``workflow_version_history`` panel. The panel is the only UI surface for
      navigating to older workflow versions, so each per-version dict carries
      run count, compact history policy label, locked/archived qualifiers,
      and timestamps.
    - ``has_workflow_versions``: convenience flag for templates that only
      render the panel when there is more than one sibling.

    Run counts are computed with a single GROUPED COUNT keyed by workflow
    PK rather than per-row ``.has_runs()`` lookups, so the panel scales to
    families with many versions without an N+1.
    """

    versions = get_visible_workflow_family(workflow=workflow, user=request.user)
    latest_workflow = get_latest_workflow(
        Workflow.objects.filter(
            org=workflow.org,
            slug=workflow.slug,
            pk__in=[version.pk for version in versions],
        ),
        include_archived=True,
    )
    run_counts = _build_run_counts(versions)
    version_options = [
        {
            "id": version.pk,
            "version": version.version or "unversioned",
            "label": _version_label(version),
            "url": reverse_with_org(
                "workflows:workflow_detail",
                request=request,
                kwargs={"pk": version.pk},
            ),
            "is_current": version.pk == workflow.pk,
            "is_latest": bool(latest_workflow and latest_workflow.pk == version.pk),
            "is_active": version.is_active,
            "is_archived": version.is_archived,
            "is_tombstoned": version.is_tombstoned,
            "is_locked": version.is_locked,
            "run_count": run_counts.get(version.pk, 0),
            "has_runs": run_counts.get(version.pk, 0) > 0,
            "history_policy": _compact_history_policy_label(version.history_policy),
            "created": version.created,
            "modified": version.modified,
        }
        for version in versions
    ]
    latest_workflow_version = (
        _version_number(latest_workflow) if latest_workflow else ""
    )
    return {
        "workflow_versions": version_options,
        "workflow_version_count": len(version_options),
        "has_workflow_versions": len(version_options) > 1,
        "latest_workflow": latest_workflow,
        "latest_workflow_version": latest_workflow_version,
        "is_latest_workflow_version": bool(
            latest_workflow and latest_workflow.pk == workflow.pk
        ),
        "workflow_version_badge": _build_header_version_badge(
            workflow=workflow,
            latest_workflow=latest_workflow,
        ),
    }


def build_workflow_breadcrumb_item(
    *,
    workflow: Workflow,
    url: str = "",
    latest_workflow: Workflow | None = None,
) -> dict[str, Any]:
    """Return a breadcrumb dict for a workflow row with a compact version badge."""

    if latest_workflow is None:
        latest_workflow = get_latest_workflow(
            Workflow.objects.filter(org_id=workflow.org_id, slug=workflow.slug),
            include_archived=True,
        )
    return {
        "name": workflow.name,
        "url": url,
        "version_badge": _build_header_version_badge(
            workflow=workflow,
            latest_workflow=latest_workflow,
            is_small=True,
        ),
    }


def build_workflow_list_version_badges(
    *,
    request: HttpRequest,
    workflows: list[Workflow],
) -> dict[int, list[dict[str, Any]]]:
    """Build compact version links for workflows shown in list surfaces.

    Workflow list pages collapse a family to one displayed row. These badge
    options preserve direct navigation to each visible sibling version without
    making templates perform per-row family lookups.
    """

    if not workflows:
        return {}

    family_keys = {(workflow.org_id, workflow.slug) for workflow in workflows}
    org_ids = {org_id for org_id, _slug in family_keys}
    slugs = {slug for _org_id, slug in family_keys}
    user = request.user

    if getattr(user, "is_authenticated", False) and getattr(
        user,
        "is_superuser",
        False,
    ):
        queryset = Workflow.objects.filter(org_id__in=org_ids, slug__in=slugs)
    else:
        queryset = Workflow.objects.for_user(user).filter(
            org_id__in=org_ids,
            slug__in=slugs,
        )

    versions = [
        version
        for version in queryset.filter(is_tombstoned=False).select_related("org")
        if (version.org_id, version.slug) in family_keys
    ]
    versions_by_family: dict[tuple[int, str], list[Workflow]] = defaultdict(list)
    for version in versions:
        versions_by_family[(version.org_id, version.slug)].append(version)
    for family_versions in versions_by_family.values():
        family_versions.sort(key=_version_sort_key, reverse=True)

    badges_by_workflow: dict[int, list[dict[str, Any]]] = {}
    for workflow in workflows:
        family_versions = versions_by_family.get((workflow.org_id, workflow.slug), [])
        if not family_versions:
            family_versions = [workflow]
        badges_by_workflow[workflow.pk] = [
            _build_version_badge(
                version=version,
                is_current=version.pk == workflow.pk,
                title=_("View version %(version)s")
                % {"version": _version_number(version)},
                url=reverse_with_org(
                    "workflows:workflow_detail",
                    request=request,
                    kwargs={"pk": version.pk},
                ),
            )
            for version in family_versions
        ]

    return badges_by_workflow


def workflow_is_latest_version(workflow: Workflow) -> bool:
    """Return whether ``workflow`` is the latest version in its family."""

    latest = get_latest_workflow(
        Workflow.objects.filter(org=workflow.org, slug=workflow.slug),
        include_archived=True,
    )
    return bool(latest and latest.pk == workflow.pk)


def _build_run_counts(versions: list[Workflow]) -> dict[int, int]:
    """Return ``{workflow_pk: run_count}`` for the supplied versions.

    Uses a single GROUP BY aggregate so the version history panel does not
    issue one query per row. Versions with zero runs simply don't appear
    in the returned dict; callers should treat a missing key as zero.
    """
    from django.db.models import Count

    from validibot.validations.models import ValidationRun

    if not versions:
        return {}
    pks = [version.pk for version in versions]
    rows = (
        ValidationRun.objects.filter(workflow_id__in=pks)
        .values("workflow_id")
        .annotate(count=Count("id"))
    )
    return {row["workflow_id"]: row["count"] for row in rows}


def _compact_history_policy_label(history_policy: str) -> str:
    """Return a short table label for a workflow history policy."""

    if history_policy == WorkflowHistoryPolicy.MUTABLE:
        return "Mutable"
    return "Versioned"


def _version_sort_key(workflow: Workflow) -> tuple[int, float, int]:
    return (
        parse_workflow_version(workflow.version),
        workflow.created.timestamp(),
        workflow.pk,
    )


def _version_label(workflow: Workflow) -> str:
    label = f"v{workflow.version}" if workflow.version else "unversioned"
    qualifiers: list[str] = []
    if workflow.is_archived:
        qualifiers.append("archived")
    if not workflow.is_active:
        qualifiers.append("inactive")
    if workflow.is_locked:
        qualifiers.append("locked")
    if qualifiers:
        label = f"{label} ({', '.join(qualifiers)})"
    return label


def _version_number(workflow: Workflow) -> str:
    if not workflow.version:
        return "unversioned"
    return str(workflow.version)


def _version_badge_label(workflow: Workflow) -> str:
    version = _version_number(workflow)
    if version == "unversioned":
        return version
    return f"v{version}"


def _build_header_version_badge(
    *,
    workflow: Workflow,
    latest_workflow: Workflow | None,
    is_small: bool = False,
) -> dict[str, Any]:
    is_latest = bool(latest_workflow and latest_workflow.pk == workflow.pk)
    if is_latest:
        title = _("Latest version")
    else:
        latest_version = _version_number(latest_workflow) if latest_workflow else "?"
        title = _("Previous version. The current version is v%(version)s") % {
            "version": latest_version,
        }
    return _build_version_badge(
        version=workflow,
        is_current=is_latest,
        title=title,
        is_small=is_small,
    )


def _build_version_badge(
    *,
    version: Workflow,
    is_current: bool,
    title: str,
    url: str = "",
    is_small: bool = False,
) -> dict[str, Any]:
    return {
        "id": version.pk,
        "version": _version_number(version),
        "label": _version_badge_label(version),
        "url": url,
        "is_current": is_current,
        "is_small": is_small,
        "title": title,
    }


__all__ = [
    "build_workflow_breadcrumb_item",
    "build_workflow_list_version_badges",
    "build_workflow_version_context",
    "get_visible_workflow_family",
    "workflow_is_latest_version",
]

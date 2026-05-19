"""Workflow version-family helpers for UI and API surfaces.

Workflow versions are separate rows that share ``(org, slug)``. Views should
present that family consistently: newest version first, the current row marked,
and archived or inactive rows labelled without hiding the fact that they are
real historical definitions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from validibot.core.utils import reverse_with_org
from validibot.workflows.models import Workflow
from validibot.workflows.version_utils import ParsedVersion
from validibot.workflows.version_utils import get_latest_workflow

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
    """Build template context for a workflow version switcher."""

    versions = get_visible_workflow_family(workflow=workflow, user=request.user)
    latest = get_latest_workflow(
        Workflow.objects.filter(
            org=workflow.org,
            slug=workflow.slug,
            pk__in=[version.pk for version in versions],
        ),
        include_archived=True,
    )
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
            "is_latest": bool(latest and latest.pk == version.pk),
            "is_active": version.is_active,
            "is_archived": version.is_archived,
            "is_locked": version.is_locked,
            "has_runs": version.has_runs(),
            "history_policy": version.get_history_policy_display(),
        }
        for version in versions
    ]
    return {
        "workflow_versions": version_options,
        "workflow_version_count": len(version_options),
        "has_workflow_versions": len(version_options) > 1,
        "latest_workflow_version": latest,
        "is_latest_workflow_version": bool(latest and latest.pk == workflow.pk),
    }


def _version_sort_key(workflow: Workflow) -> tuple[tuple[int, int, int], float, int]:
    parsed = ParsedVersion(workflow.version or "")
    return (parsed.as_tuple(), workflow.created.timestamp(), workflow.pk)


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


__all__ = ["build_workflow_version_context", "get_visible_workflow_family"]

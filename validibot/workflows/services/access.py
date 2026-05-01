"""WorkflowAccessResolver — single decision point for workflow visibility.

ADR-2026-04-27 Phase 2: object-level workflow access decisions live in
one place so the four launch paths (web, REST API, MCP helper API,
x402) and the discovery surfaces (workflow list, workflow detail,
runs list) all answer "is this workflow visible to this user?" the
same way.

Why a resolver instead of a permission class
============================================

Django's permission system is great for "is this request method
allowed?" decisions but awkward for object-level scoping that needs
to combine role-based access (org member with WORKFLOW_VIEW
permission), object-grant access (guest with a WorkflowAccessGrant),
and creator access (the user who made the workflow). Phase 0's
``[trust-#1]`` fix wired ``Workflow.objects.for_user(...)`` into
``OrgScopedWorkflowViewSet.get_queryset()`` to close one specific
hole, but several other call sites still build their own ad-hoc
querysets.

This module makes the queryset-building canonical. Every caller
imports the resolver; nobody hand-rolls a queryset against
``Workflow.objects`` directly with role/membership/grant filters.

What's in scope vs. out of scope
================================

In scope:
- "Can this user list these workflows?" -> queryset filter
- "Can this user retrieve this specific workflow?" -> single-object lookup
- "Is this workflow active and not tombstoned?" -> readiness check

Out of scope (lives elsewhere):
- "Can this user *launch* this workflow with this payload?" -> that's
  a two-step decision: first the access check (here), then the launch
  contract (``LaunchContract.validate``). Separating them lets a
  permission-denied user never hit the contract check.
- Latest-version selection for public agent paths -> that's
  ``AgentWorkflowResolver`` (sibling module). Different audience
  (anonymous agents vs. authenticated users), different rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q
from django.shortcuts import get_object_or_404

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser
    from django.db.models import QuerySet

    from validibot.workflows.models import Workflow


class WorkflowAccessResolver:
    """Single decision point for object-level workflow access.

    Stateless — methods are static. We use a class rather than module-
    level functions for two reasons:

    1. The import name (``WorkflowAccessResolver.list_for_user(...)``)
       is more searchable than a free function. ``rg
       WorkflowAccessResolver`` finds every caller; ``rg
       list_workflows_for_user`` collides with other helpers.
    2. Future extensions (object-level write access, agent-public
       discovery filtering) have a natural home on the same class.

    All methods accept ``user`` as the first positional argument and
    return either a queryset (for list/listish operations) or a single
    workflow (for retrieve operations). None of the methods raise on
    "not found" — callers translate misses to their path-specific 404
    response. This matches the pattern used by ``LaunchContract``: the
    decision is structural, the response shape is path-specific.
    """

    @staticmethod
    def list_for_user(
        user: AbstractBaseUser,
        *,
        org_id: int | None = None,
        include_archived: bool = False,
        include_inactive: bool = False,
        include_tombstoned: bool = False,
    ) -> QuerySet[Workflow]:
        """Return all workflows visible to ``user``, optionally org-scoped.

        Combines the three access mechanisms documented on
        ``WorkflowQuerySet.for_user``:

        - Org membership with WORKFLOW_VIEW permission
        - Workflow creator
        - Active WorkflowAccessGrant (guests)

        The lifecycle fields ``is_active``, ``is_archived``, and
        ``is_tombstoned`` are three distinct concepts:

        - ``is_active=False`` -> workflow exists but cannot be
          launched. Still visible in the org admin so an admin can
          re-activate it.
        - ``is_archived=True`` -> workflow is hidden from default
          listings but still launchable by people who have the URL
          (e.g. existing scheduled tasks).
        - ``is_tombstoned=True`` -> soft-deleted. Should be invisible
          to everyone except staff-only recovery flows.

        Default filters apply all three (no archived, no inactive,
        no tombstoned) because that's what user-facing list views
        want. Each ``include_*`` flag lifts one filter.

        Args:
            user: The user requesting access.
            org_id: When provided, restrict the result to workflows
                in this org. The org-scoped REST API view passes
                this from the URL kwarg.
            include_archived: Lift the ``is_archived=False`` filter.
                Used by admin surfaces.
            include_inactive: Lift the ``is_active=True`` filter.
                Used by admin / staff surfaces.
            include_tombstoned: Lift the ``is_tombstoned=False``
                filter. Used by staff recovery surfaces only.

        Returns:
            A ``QuerySet[Workflow]`` filtered to workflows visible
            to the user. Empty for unauthenticated users (matching
            ``Workflow.objects.for_user(...)``'s contract).
        """
        # Local import to avoid a circular import (services
        # import models, and models in turn pull in some service-
        # level helpers via signals).
        from validibot.workflows.models import Workflow

        qs = Workflow.objects.for_user(user)

        if org_id is not None:
            qs = qs.filter(org_id=org_id)

        if not include_inactive:
            qs = qs.filter(is_active=True)

        if not include_archived:
            # Some legacy rows may have ``is_archived=NULL``; treat
            # both as "not archived" so we don't silently exclude
            # pre-migration rows.
            qs = qs.filter(Q(is_archived=False) | Q(is_archived__isnull=True))

        if not include_tombstoned:
            qs = qs.filter(Q(is_tombstoned=False) | Q(is_tombstoned__isnull=True))

        return qs

    @staticmethod
    def get_for_user(
        user: AbstractBaseUser,
        *,
        slug: str | None = None,
        pk: int | None = None,
        org_id: int | None = None,
        include_archived: bool = False,
        include_inactive: bool = False,
    ) -> Workflow | None:
        """Return a single workflow visible to ``user``, or None if not found.

        Pass exactly one of ``slug`` or ``pk``. Slug lookups are the
        common case for user-facing URLs (``/workflows/<slug>/``);
        pk lookups appear in API URLs and admin contexts.

        For slug lookups across versioned workflows: this resolver
        returns the latest version visible to the user. Earlier
        versions remain accessible by pk via the version-specific
        viewset (``WorkflowVersionViewSet``).

        Args:
            user: The user requesting access.
            slug: Workflow slug. When the slug is shared across
                multiple versions, returns the latest active version
                visible to the user.
            pk: Workflow primary key. Returns the exact row.
            org_id: When provided, restrict the lookup to this org.
            include_inactive: Lift the ``is_active=True`` filter for
                admin / recovery flows.

        Returns:
            The matching :class:`Workflow`, or None if no workflow
            matches the criteria AND is visible to the user.

        Raises:
            ValueError: If neither slug nor pk is provided, or both.
        """
        # Local import — see ``list_for_user``.
        from validibot.workflows.version_utils import get_latest_workflow

        if (slug is None) == (pk is None):
            msg = "Pass exactly one of slug or pk."
            raise ValueError(msg)

        qs = WorkflowAccessResolver.list_for_user(
            user,
            org_id=org_id,
            include_archived=include_archived,
            include_inactive=include_inactive,
        )

        if pk is not None:
            return qs.filter(pk=pk).first()

        # Slug lookup — versioned workflows share a slug, so we
        # narrow to all versions matching the slug then pick the
        # latest. ``get_latest_workflow`` returns None on empty input.
        candidates = qs.filter(slug=slug).select_related("org")
        return get_latest_workflow(candidates)

    @staticmethod
    def get_or_404(
        user: AbstractBaseUser,
        *,
        slug: str | None = None,
        pk: int | None = None,
        org_id: int | None = None,
        include_inactive: bool = False,
    ) -> Workflow:
        """Like :meth:`get_for_user` but raises Django's Http404.

        Convenience for view callers that want the "raise 404 on
        miss" behaviour (most Django views). Service callers and
        custom error handlers should prefer :meth:`get_for_user` to
        keep error mapping in their hands.
        """
        # We pass a callable that uses our queryset machinery. We
        # can't use Django's ``get_object_or_404`` directly because
        # it doesn't know about latest-version selection.
        workflow = WorkflowAccessResolver.get_for_user(
            user,
            slug=slug,
            pk=pk,
            org_id=org_id,
            include_inactive=include_inactive,
        )
        if workflow is None:
            # Use Django's 404 mechanism so DRF / framework handlers
            # produce the standard 404 envelope.
            from validibot.workflows.models import Workflow

            # The actual queryset doesn't matter — get_object_or_404
            # is being used purely to raise Http404 with a sensible
            # message. We pass an empty queryset to guarantee the
            # raise.
            get_object_or_404(Workflow.objects.none())
        return workflow  # type: ignore[return-value]


__all__ = ["WorkflowAccessResolver"]

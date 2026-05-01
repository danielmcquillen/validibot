"""AgentWorkflowResolver — latest-version selection for public agent paths.

ADR-2026-04-27 Phase 2: the public agent paths (MCP helper API, x402
cloud agent, future REST agent endpoints) need to resolve a workflow
slug to its latest active version, *without* a user identity (these
paths are anonymous or service-to-service). Phase 0's ``[trust-#2]``
fix added ``get_latest_workflow(...)`` to the x402 path; this
resolver generalises that selection rule so the discovery list, the
detail view, and the run-create path all agree on which version a
slug resolves to.

Why a separate resolver from WorkflowAccessResolver
===================================================

User-authenticated paths (community web, REST API, MCP for members)
filter by user identity; agent paths filter by *publishing* state
(``agent_public_discovery``, ``agent_access_enabled``,
``agent_billing_mode``). The two have different security models and
different "is this visible?" semantics:

- WorkflowAccessResolver answers: "is this user allowed to see /
  launch this workflow?"
- AgentWorkflowResolver answers: "has the workflow owner published
  this workflow for anonymous agent discovery, and which version
  should anonymous agents see?"

Conflating them in one resolver would force every method to take
both ``user`` and ``agent_context`` and branch internally — confusing
and error-prone. Two resolvers, one contract each.

What's in scope vs. out of scope
================================

In scope:
- Latest-version selection across a workflow family
- ``agent_public_discovery=True`` filter
- ``is_active=True``, ``is_tombstoned=False`` filters

Out of scope:
- Per-step compatibility / file-type checks -> ``LaunchContract``
- Payment / billing-mode validation -> caller's responsibility
  (x402's ``_ensure_public_x402_workflow`` checks ``agent_billing_mode``
  and ``agent_access_enabled`` separately because those are payment-
  related, not discovery-related)
- Rate limiting -> caller's responsibility
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.workflows.models import Workflow


class AgentWorkflowResolver:
    """Single decision point for public-agent workflow discovery.

    Stateless — methods are static. See sibling
    :class:`WorkflowAccessResolver` for the rationale on class-vs-
    free-functions and the no-raise-on-miss policy.

    The resolver focuses on *discovery* and *resolution* — what
    workflows exist for an anonymous agent to see, and which version
    a slug resolves to. Per-call validation (file type, step compat,
    payment) lives in their own services.
    """

    @staticmethod
    def list_published() -> list[Workflow]:
        """Return all workflows published for anonymous agent discovery.

        A workflow is published when:

        - ``is_active=True``
        - ``is_tombstoned=False`` (or null)
        - ``agent_public_discovery=True``
        - It's the latest active version of its slug

        The "latest active version" filter matters because a workflow
        family with v1, v2, and v3 should appear once in the listing
        (as v3), not three times. The latest-version selection
        reuses ``get_latest_workflow_ids`` from version_utils.

        Returns:
            A list of :class:`Workflow` instances, latest-version
            only, sorted by org then name. Returns a list (not a
            queryset) because the latest-version filter requires a
            secondary query that's awkward to express as a single
            queryset; the caller usually iterates the result anyway.
        """
        # Local import — see WorkflowAccessResolver.list_for_user.
        from validibot.workflows.models import Workflow
        from validibot.workflows.version_utils import get_latest_workflow_ids

        candidates_qs = Workflow.objects.select_related("org").filter(
            is_active=True,
            agent_public_discovery=True,
        )
        # Some legacy rows may have ``is_tombstoned=NULL`` rather than
        # ``False``. Treat both as "not tombstoned" so we don't
        # silently exclude pre-migration rows.
        candidates_qs = candidates_qs.exclude(is_tombstoned=True)

        latest_ids = get_latest_workflow_ids(candidates_qs)
        return list(
            Workflow.objects.select_related("org")
            .filter(pk__in=latest_ids)
            .order_by("org__slug", "name"),
        )

    @staticmethod
    def get_by_slug(
        *,
        org_slug: str,
        workflow_slug: str,
    ) -> Workflow | None:
        """Return the latest active published workflow matching slug.

        Used by both the public agent detail view and the run-create
        path — Phase 0's ``[trust-#2]`` fix applied the same selection
        rule in both places, so this resolver gives them a single
        source of truth.

        Args:
            org_slug: Org slug from the agent context (URL or x402
                metadata header).
            workflow_slug: Workflow slug from the agent context.

        Returns:
            The latest active version of the matching workflow, or
            None if no published version exists. Callers translate
            None to their path-specific 404 response.
        """
        # Local import — see WorkflowAccessResolver.list_for_user.
        from validibot.workflows.models import Workflow
        from validibot.workflows.version_utils import get_latest_workflow

        candidates = Workflow.objects.select_related("org").filter(
            slug=workflow_slug,
            org__slug=org_slug,
            is_active=True,
            agent_public_discovery=True,
        )
        candidates = candidates.exclude(is_tombstoned=True)
        return get_latest_workflow(candidates)

    @staticmethod
    def get_by_slug_for_x402(
        *,
        org_slug: str,
        workflow_slug: str,
    ) -> Workflow | None:
        """Return the latest active workflow matching slug, x402-relaxed.

        Variant of :meth:`get_by_slug` that does NOT filter on
        ``agent_public_discovery``. Used by x402's ``_resolve_workflow``
        which has its own publishing checks (``_ensure_public_x402_workflow``
        verifies ``agent_public_discovery`` AND ``agent_access_enabled``
        AND ``agent_billing_mode``) — separating discovery from
        resolution lets x402's error envelope distinguish "not found"
        from "not published for x402".

        This split exists because x402 wants different error codes
        for "no workflow with this slug" vs. "workflow exists but
        isn't published for x402 payments". The MCP / public-discovery
        path doesn't need that distinction — both cases just produce
        404.

        Args:
            org_slug: Org slug from x402 metadata.
            workflow_slug: Workflow slug from x402 metadata.

        Returns:
            The latest active version of the matching workflow,
            irrespective of ``agent_public_discovery``. None if no
            active version exists.
        """
        # Local import — see WorkflowAccessResolver.list_for_user.
        from validibot.workflows.models import Workflow
        from validibot.workflows.version_utils import get_latest_workflow

        candidates = Workflow.objects.select_related("org").filter(
            slug=workflow_slug,
            org__slug=org_slug,
            is_active=True,
        )
        candidates = candidates.exclude(is_tombstoned=True)
        return get_latest_workflow(candidates)


__all__ = ["AgentWorkflowResolver"]

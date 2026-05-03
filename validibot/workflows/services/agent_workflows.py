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

from django.db.models import Q

if TYPE_CHECKING:
    from validibot.workflows.models import Workflow


def _public_x402_predicate() -> Q:
    """Return the full filter for "publicly discoverable, valid x402 workflow".

    The resolver SHOULD have a defensive filter that enforces every
    invariant the publishing decision depends on, not just the
    ``agent_public_discovery`` flag. ``Workflow.clean()`` enforces
    these as a unit, but ``clean()`` doesn't fire on
    ``QuerySet.update()``, on fixtures (``loaddata``), or on
    admin-side bulk paths. The ADR called for matching DB
    constraints; until those land, the resolver is the next-best
    guard.

    The full publishing contract (mirroring what
    ``Workflow.clean()`` enforces):

    1. Lifecycle: ``is_active=True``, not ``is_tombstoned``,
       not ``is_archived``.
    2. Public-discovery flag is on: ``agent_public_discovery=True``.
    3. Agent access is enabled (the runtime gate):
       ``agent_access_enabled=True``.
    4. Billing mode is x402: ``agent_billing_mode=AGENT_PAYS_X402``.
       Other billing modes don't expose anonymous agents to a
       payment flow, so they shouldn't appear in the public catalog.
    5. Price is set (positive): ``agent_price_cents > 0``. A
       price of 0 or null means the row was created mid-config and
       doesn't yet describe a paying transaction.
    6. Retention invariant for x402: ``input_retention=DO_NOT_STORE``.
       x402 is anonymous per-call payment; storing the input would
       undermine the privacy model the operator agreed to. The
       form-level cascade enforces this for human-driven edits;
       the resolver enforces it for every other edit path.

    Some legacy rows may have ``is_archived=NULL`` or
    ``is_tombstoned=NULL`` — treat both as "not archived" / "not
    tombstoned" so we don't silently exclude pre-migration rows.
    """
    # Local import — see ``AgentWorkflowResolver.list_published``.
    from validibot.submissions.constants import SubmissionRetention
    from validibot.workflows.constants import AgentBillingMode

    return (
        Q(is_active=True)
        & Q(agent_public_discovery=True)
        & Q(agent_access_enabled=True)
        & Q(agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402)
        & Q(agent_price_cents__gt=0)
        & Q(input_retention=SubmissionRetention.DO_NOT_STORE)
        & (Q(is_tombstoned=False) | Q(is_tombstoned__isnull=True))
        & (Q(is_archived=False) | Q(is_archived__isnull=True))
    )


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

        A workflow is published when every clause in
        :func:`_public_x402_predicate` holds AND it's the latest
        active version of its slug.

        ``Workflow.clean()`` enforces those clauses as a unit, but
        ``clean()`` doesn't fire on ``QuerySet.update()``, fixtures,
        or admin-side bulk paths. The resolver applies the full
        predicate as a defensive filter so a row created via one of
        those paths does NOT leak into the public catalog even
        though x402 run creation would later reject it.

        Returns:
            A list of :class:`Workflow` instances, latest-version
            only, sorted by org then name.
        """
        # Local import — see WorkflowAccessResolver.list_for_user.
        from validibot.workflows.models import Workflow
        from validibot.workflows.version_utils import get_latest_workflow_ids

        candidates_qs = Workflow.objects.select_related("org").filter(
            _public_x402_predicate(),
        )

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

        Applies the same defensive predicate as :meth:`list_published`
        (see :func:`_public_x402_predicate` for the full clause list)
        so a workflow row that satisfies ``agent_public_discovery=True``
        but fails any other publishing invariant (e.g. price=0,
        billing_mode wrong, retention not DO_NOT_STORE) does not
        appear as a valid x402 target on the detail surface.

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

        candidates = (
            Workflow.objects.select_related("org")
            .filter(_public_x402_predicate())
            .filter(slug=workflow_slug, org__slug=org_slug)
        )
        return get_latest_workflow(candidates)

    @staticmethod
    def is_valid_public_x402_publish(workflow: Workflow) -> bool:
        """Return True iff ``workflow`` currently satisfies every public-x402 invariant.

        Trust ADR (2026-04-27) + 2026-05-03 review (P1 #5): the
        x402 run-creation path was using a relaxed subset of the
        publishing predicate (only ``agent_public_discovery``,
        ``agent_access_enabled``, and ``billing_mode``). That
        meant archived rows, zero-price rows, and rows whose
        ``input_retention`` was not ``DO_NOT_STORE`` could pass the
        relaxed gate (when those rows skipped ``clean()``) and
        create runs against a workflow that wouldn't actually
        appear in the public catalog.

        This method re-applies the **full**
        :func:`_public_x402_predicate` against the single workflow's
        current state at run-create time, closing the gap. Callers
        in agent run creation should treat ``False`` as "this row
        is no longer a valid public-x402 publish; do not create
        runs against it."

        Implementation note: re-runs the predicate as a database
        filter (rather than evaluating field-by-field on the
        Python side) so concurrent admin edits to the workflow are
        observed atomically. The cost is a single PK-bounded
        query.
        """
        # Local import — see WorkflowAccessResolver.list_for_user.
        from validibot.workflows.models import Workflow

        return Workflow.objects.filter(
            _public_x402_predicate(),
            pk=workflow.pk,
        ).exists()

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

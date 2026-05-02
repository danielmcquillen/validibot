"""WorkflowVersioningService — the single decision point for workflow version cloning.

ADR-2026-04-27 Phase 3: workflow versioning was historically a single
``Workflow.clone_to_new_version()`` method that copied the workflow,
its steps, and its step resources — but missed
``WorkflowPublicInfo``, ``WorkflowRoleAccess``, and
``WorkflowSignalMapping``. The result: a "new version" silently
inherited some pre-clone state from the old version, which violates
the trust-boundary principle that a workflow with a run cannot
silently change its launch contract.

This service replaces the inline clone logic with a structured
service that:

1. Copies the **complete** workflow contract — every related object
   that defines what a launched run was operating under.
2. Returns a structured :class:`CloneReport` so callers (and tests)
   can verify *what* got copied and *how many* of each component.
3. Centralises the policy decision of "what counts as the contract"
   in one place. Adding a new related object becomes a single edit
   here, not a four-place edit across model, form, serializer, and
   admin.

What's in scope vs. out of scope
================================

In scope (this module):

- Workflow row fields (every contract field listed in
  :data:`CONTRACT_FIELDS`)
- :class:`WorkflowStep` rows including the ``config`` JSONField
- :class:`WorkflowStepResource` rows (both catalog references and
  step-owned files)
- :class:`WorkflowPublicInfo` (the public-facing info page)
- :class:`WorkflowRoleAccess` (per-role permission grants)
- :class:`WorkflowSignalMapping` (cross-step signal flow)

Out of scope (deferred to later Phase 3 sessions):

- **Ruleset immutability** (Phase 3 Session C, task 10). For now the
  cloned step references the same ``Ruleset`` row as the source — if
  someone mutates the ruleset, both versions see the change. Session
  C adds the immutability check.
- **Validator semantic digest** (Phase 3 Session B, tasks 7–9). The
  cloned step references the same ``Validator`` row; we don't yet
  capture a snapshot of the validator's behavior at clone time.
- **Resource-file hashing** (Phase 3 Session C, task 11). Step-owned
  resource file content is copied today, so this is partially
  addressed; the catalog-reference path will gain hash enforcement
  in Session C.

The structured report's ``warnings`` field surfaces these gaps to the
caller so operators using the API today know what their "new version"
does and does not yet protect against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

from django.core.files.base import ContentFile
from django.db import transaction

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

    from validibot.workflows.models import Workflow

logger = logging.getLogger(__name__)


# Fields that constitute the "launch contract" — the rules under
# which a previously-launched run was operating. Editing any of
# these on a workflow that has runs (or is locked) requires a new
# version, not an in-place edit. See
# :meth:`Workflow.requires_new_version_for_contract_edits`.
#
# What's NOT contract:
#   - name, description (cosmetic)
#   - is_active, is_archived, is_tombstoned (lifecycle flags;
#     editing them changes which workflows are *visible*, not the
#     rules of any past run)
#   - is_locked (a marker, not a contract field)
#   - slug, version (identifiers; changing requires care but is a
#     separate concern)
#   - make_info_page_public, featured_image (presentation)
CONTRACT_FIELDS = frozenset(
    {
        "allowed_file_types",
        "input_retention",
        "output_retention",
        "agent_billing_mode",
        "agent_price_cents",
        "agent_max_launches_per_hour",
        "agent_public_discovery",
        "agent_access_enabled",
    },
)


@dataclass(frozen=True)
class CloneReport:
    """Structured result of a workflow clone operation.

    Returned by :meth:`WorkflowVersioningService.clone` so callers
    (and tests) can verify exactly what got copied and surface gaps
    to operators. Fields:

    Attributes:
        source_workflow_id: PK of the source workflow.
        new_workflow_id: PK of the newly-created workflow row.
        new_version_label: The version string assigned to the clone
            (e.g. ``"2"``, ``"1.0.1"``).
        components_copied: Per-component counts. Keys are short
            names (``steps``, ``step_resources``, ``public_info``,
            ``role_access``, ``signal_mappings``); values are counts.
            Useful for asserting in tests that the clone didn't
            silently skip any component.
        warnings: Free-form messages about partial copies — e.g.
            "ruleset references not yet immutable (Phase 3 Session
            C)". Visible to operators via API responses and to
            developers via logs.
    """

    source_workflow_id: int
    new_workflow_id: int
    new_version_label: str
    components_copied: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class WorkflowVersioningService:
    """The single decision point for workflow version cloning.

    Stateless — methods are static. See sibling
    :class:`validibot.workflows.services.access.WorkflowAccessResolver`
    for the rationale on class-vs-free-functions.

    The service deliberately does NOT mark the source workflow as
    locked here. The existing ``Workflow.clone_to_new_version()``
    method does that as part of its contract; preserving that
    behaviour means existing callers don't notice the refactor.
    Tests verify the locking still happens.
    """

    @staticmethod
    @transaction.atomic
    def clone(workflow: Workflow, *, user: AbstractBaseUser) -> CloneReport:
        """Clone ``workflow`` to a new version, copying the complete contract.

        Performs the following copy operations in order:

        1. Determine the next version label.
        2. Create the new ``Workflow`` row with all contract fields.
        3. Copy ``WorkflowStep`` rows (including ``config`` JSONField).
        4. Copy ``WorkflowStepResource`` rows for each new step.
        5. Copy ``WorkflowPublicInfo`` (one-to-one).
        6. Copy ``WorkflowRoleAccess`` rows.
        7. Copy ``WorkflowSignalMapping`` rows.
        8. Lock the source workflow.

        All operations run inside a single transaction so a failure
        midway leaves the database unchanged — no half-cloned
        workflows.

        Args:
            workflow: The source workflow to clone.
            user: The user performing the clone (recorded as the
                new workflow's ``user`` field for audit purposes).

        Returns:
            A :class:`CloneReport` with the new workflow's ID,
            version label, per-component copy counts, and any
            warnings about partial-copy gaps.
        """
        # Local imports to avoid a circular import (services
        # import models, and models in turn pull in some service-
        # level helpers via signals).
        from validibot.workflows.models import Workflow
        from validibot.workflows.models import WorkflowPublicInfo
        from validibot.workflows.models import WorkflowRoleAccess
        from validibot.workflows.models import WorkflowSignalMapping
        from validibot.workflows.models import WorkflowStep
        from validibot.workflows.models import WorkflowStepResource

        components_copied: dict[str, int] = {}
        warnings: list[str] = []

        # 1. Determine next version label
        sibling_versions = list(
            Workflow.objects.filter(org=workflow.org, slug=workflow.slug)
            .exclude(pk=workflow.pk)
            .values_list("version", flat=True),
        )
        sibling_versions.append(workflow.version)
        # Reuse the existing private helper on Workflow for
        # version-label arithmetic — that's pure-data logic that
        # doesn't need to move into the service yet.
        next_version = workflow._determine_next_version_label(sibling_versions)

        # 2. Create the new workflow row with all contract fields
        new_workflow = Workflow.objects.create(
            org=workflow.org,
            user=user,
            name=workflow.name,
            slug=workflow.slug,
            version=next_version,
            is_locked=False,
            is_active=workflow.is_active,
            allowed_file_types=list(workflow.allowed_file_types or []),
            input_retention=workflow.input_retention,
            output_retention=workflow.output_retention,
            # Phase 3: copy the agent-launch contract too. The
            # historical clone_to_new_version omitted these,
            # meaning a new version of an x402-published workflow
            # silently lost its publication state.
            agent_billing_mode=workflow.agent_billing_mode,
            agent_price_cents=workflow.agent_price_cents,
            agent_max_launches_per_hour=workflow.agent_max_launches_per_hour,
            agent_public_discovery=workflow.agent_public_discovery,
            agent_access_enabled=workflow.agent_access_enabled,
        )

        # 3. + 4. Steps and step resources.
        # Snapshot original steps and their step_resources before
        # mutating .pk = None, because prefetch_related results
        # are cached on the Python objects.
        original_steps = list(
            workflow.steps.prefetch_related("step_resources").order_by("order"),
        )
        old_pks = [step.pk for step in original_steps]
        step_resource_map = {
            step.pk: list(step.step_resources.all()) for step in original_steps
        }

        for step in original_steps:
            step.pk = None
            step.workflow = new_workflow
        WorkflowStep.objects.bulk_create(original_steps)
        components_copied["steps"] = len(original_steps)

        step_resource_count = 0
        for old_pk, new_step in zip(old_pks, original_steps, strict=True):
            for old_res in step_resource_map.get(old_pk, []):
                if old_res.is_catalog_reference:
                    WorkflowStepResource.objects.create(
                        step=new_step,
                        role=old_res.role,
                        validator_resource_file=old_res.validator_resource_file,
                    )
                    step_resource_count += 1
                else:
                    # Use a context manager to avoid file-handle leaks
                    # if read() or the subsequent create() raises.
                    with old_res.step_resource_file.open("rb") as f:
                        file_content = f.read()
                    WorkflowStepResource.objects.create(
                        step=new_step,
                        role=old_res.role,
                        step_resource_file=ContentFile(
                            file_content,
                            name=old_res.filename or "file",
                        ),
                        filename=old_res.filename,
                        resource_type=old_res.resource_type,
                    )
                    step_resource_count += 1
        components_copied["step_resources"] = step_resource_count

        # 5. WorkflowPublicInfo (one-to-one). Skip if source has none.
        try:
            old_public_info = workflow.public_info
        except WorkflowPublicInfo.DoesNotExist:
            old_public_info = None

        if old_public_info is not None:
            old_public_info.pk = None
            old_public_info.workflow = new_workflow
            old_public_info.save()
            components_copied["public_info"] = 1
        else:
            components_copied["public_info"] = 0

        # 6. WorkflowRoleAccess
        role_access_rows = list(WorkflowRoleAccess.objects.filter(workflow=workflow))
        for ra in role_access_rows:
            ra.pk = None
            ra.workflow = new_workflow
        WorkflowRoleAccess.objects.bulk_create(role_access_rows)
        components_copied["role_access"] = len(role_access_rows)

        # 7. WorkflowSignalMapping
        signal_mappings = list(WorkflowSignalMapping.objects.filter(workflow=workflow))
        for sm in signal_mappings:
            sm.pk = None
            sm.workflow = new_workflow
        WorkflowSignalMapping.objects.bulk_create(signal_mappings)
        components_copied["signal_mappings"] = len(signal_mappings)

        # 8. Lock the source workflow.
        workflow.is_locked = True
        workflow.save(update_fields=["is_locked"])

        # Surface deferred-Phase warnings.
        if any(step.ruleset_id for step in original_steps):
            warnings.append(
                "Cloned steps reference the same Ruleset rows as the "
                "source workflow; ruleset immutability lands in Phase 3 "
                "Session C (task 10).",
            )
        if any(step.validator_id for step in original_steps):
            warnings.append(
                "Cloned steps reference the same Validator rows as the "
                "source workflow; validator semantic digest lands in "
                "Phase 3 Session B (tasks 7-9).",
            )

        report = CloneReport(
            source_workflow_id=workflow.pk,
            new_workflow_id=new_workflow.pk,
            new_version_label=next_version,
            components_copied=components_copied,
            warnings=warnings,
        )
        logger.info(
            "Cloned workflow %s (v%s) to %s (v%s): %s",
            workflow.pk,
            workflow.version,
            new_workflow.pk,
            next_version,
            components_copied,
        )
        return report


__all__ = ["CONTRACT_FIELDS", "CloneReport", "WorkflowVersioningService"]

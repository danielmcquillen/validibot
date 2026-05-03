from __future__ import annotations

import logging
import math
import re
import uuid
from typing import TYPE_CHECKING

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.files.storage import storages
from django.db import models
from django.db import transaction
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from validibot.actions.models import Action
from validibot.core.constants import InviteStatus
from validibot.core.mixins import FeaturedImageMixin
from validibot.core.utils import render_markdown_safe
from validibot.projects.models import Project
from validibot.submissions.constants import OutputRetention
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.users.models import Role
from validibot.users.models import User
from validibot.users.permissions import PermissionCode
from validibot.users.permissions import roles_for_permission
from validibot.workflows.constants import AgentBillingMode

if TYPE_CHECKING:
    from validibot.users.constants import RoleCode

logger = logging.getLogger(__name__)

# Pattern for validating semantic versions (e.g., "1", "1.0", "1.0.0")
SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)(?:\.(?P<minor>0|[1-9]\d*)(?:\.(?P<patch>0|[1-9]\d*))?)?$"
)


def validate_workflow_version(value: str) -> None:
    """
    Validate that version is either an integer or semantic version.

    Valid examples: "1", "2", "1.0", "1.0.0", "2.1.3"
    Invalid examples: "v1", "1.0.0-beta", "latest", arbitrary strings

    This ensures versions can be reliably compared and ordered.
    """
    if not value:
        return  # Empty is allowed (will be backfilled)

    # Check if it's a simple integer
    if value.isdigit():
        return

    # Check if it's a valid semantic version
    if SEMVER_PATTERN.match(value):
        return

    raise ValidationError(
        _(
            "Version must be an integer (e.g., '1') or semantic version "
            "(e.g., '1.0.0')."
        ),
    )


# DEPRECATED: select_public_storage is no longer needed.
# The default storage is now public media. This function is kept for migration
# compatibility but simply returns the default storage.
def select_public_storage():
    """Return the default storage backend (public media)."""
    return storages["default"]


class WorkflowQuerySet(models.QuerySet):
    """
    A custom queryset for Workflow model to add user-specific filtering methods.
    This lets us easily get workflows a user has access to based on their membership
    to organizations or via workflow access grants (for guests).
    """

    def for_user(
        self,
        user: User,
        required_role_code: RoleCode | None = None,
    ) -> WorkflowQuerySet:
        """
        Get workflows accessible to the given user.

        Access is granted via:
        1. Org membership with appropriate role (existing behavior)
        2. Being the workflow creator (existing behavior)
        3. Having an active WorkflowAccessGrant (new - for guests)

        If required_role is provided, only return workflows where the user
        has that role in the workflow's organization. In this case, guest
        grants are NOT included (role-specific queries are for org members).

        Otherwise, return all workflows the user can access via any of the
        three methods above.
        """
        if not getattr(user, "is_authenticated", False):
            return self.none()

        # Org membership subquery
        allowed_view_roles = roles_for_permission(PermissionCode.WORKFLOW_VIEW)
        membership_subq = Membership.objects.filter(
            org=OuterRef("org_id"),
            user=user,
            is_active=True,
        )
        if required_role_code:
            membership_subq = membership_subq.filter(roles__code=required_role_code)
        else:
            membership_subq = membership_subq.filter(roles__code__in=allowed_view_roles)

        qs = self.annotate(
            _has_membership=Exists(membership_subq),
        )

        access_filter = Q(_has_membership=True) | Q(user_id=user.id)

        # For non-role-specific queries, also include guest grant access.
        if not required_role_code:
            # ADR-2026-04-27 fix (issue #43): a guest grant targets the
            # *workflow family* — every row sharing the granted row's
            # ``(org, slug)`` pair — not just the exact pinned version
            # the grant row points at. Without this expansion, cloning
            # a workflow to v2 silently strips the guest's access until
            # someone manually re-grants on the new version row.
            #
            # The subquery joins ``WorkflowAccessGrant.workflow`` back
            # to the outer Workflow row by ``(org_id, slug)``, so any
            # active grant the user holds on ANY version of the family
            # makes every version of the family visible.
            grant_subq = WorkflowAccessGrant.objects.filter(
                user=user,
                is_active=True,
                workflow__org_id=OuterRef("org_id"),
                workflow__slug=OuterRef("slug"),
            )
            qs = qs.annotate(_has_grant=Exists(grant_subq))
            access_filter = access_filter | Q(_has_grant=True)

        return qs.filter(access_filter).distinct()


class WorkflowManager(models.Manager):
    def get_queryset(self):
        return WorkflowQuerySet(self.model, using=self._db)

    def for_user(self, user: User, required_role_code: RoleCode | None = None):
        return self.get_queryset().for_user(user, required_role_code=required_role_code)


def _default_workflow_file_types() -> list[str]:
    return [SubmissionFileType.JSON]


class Workflow(FeaturedImageMixin, TimeStampedModel):
    """
    Reusable, versioned definition of a sequence of validation steps.

    A workflow normally remains editable and launchable until it is archived or
    disabled. The break-glass delete flow adds a stronger historical-record
    state, ``is_tombstoned``, which removes the workflow from normal product
    surfaces while keeping the row available for old runs and signed
    credentials.
    """

    objects = WorkflowManager()

    featured_image = models.FileField(
        null=True,
        blank=True,
        # Use public media bucket - references STORAGES["public"] from settings
        storage=select_public_storage,
        help_text=_(
            "Optional image to represent the workflow Shown on the 'info' page.",
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "org",
                    "slug",
                    "version",
                ],
                name="uq_workflow_org_slug_version",
            ),
            # ── Trust ADR-2026-04-27 + 2026-05-03 review (P2 #1) ──
            #
            # ``Workflow.clean()`` enforces a set of trust-critical
            # invariants for the public-x402 publishing contract, but
            # ``clean()`` does not run on:
            #   • ``QuerySet.update()`` (admin bulk edits, data fixes)
            #   • Fixtures and ``loaddata``
            #   • Raw SQL writes
            # So a row can be persisted that satisfies
            # ``agent_public_discovery=True`` while violating the rest
            # of the publishing predicate. The defensive resolver
            # filter (``_public_x402_predicate``) hides such rows from
            # the catalog, but a row that exists in a contradictory
            # state is still a bug — the constraints below close that
            # last gap by enforcing each invariant at the database
            # level for every write path.
            #
            # Each constraint mirrors a clause in
            # ``_public_x402_predicate`` and the corresponding
            # ValidationError raised in ``clean()``.
            models.CheckConstraint(
                condition=(
                    Q(agent_public_discovery=False) | Q(agent_access_enabled=True)
                ),
                name="ck_workflow_public_discovery_requires_agent_access",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402)
                    | Q(agent_price_cents__gt=0)
                ),
                name="ck_workflow_x402_requires_positive_price",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402)
                    | Q(input_retention=SubmissionRetention.DO_NOT_STORE)
                ),
                name="ck_workflow_x402_requires_do_not_store_retention",
            ),
        ]
        ordering = [
            "slug",
            "-version",
        ]

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="workflows",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflows",
        help_text=_(
            "Default project to associate with runs triggered from this workflow.",
        ),
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="workflows",
        help_text=_("The user who created this workflow."),
    )

    name = models.CharField(
        max_length=200,
        blank=False,
        null=False,
        help_text=_("Name of the workflow, e.g. 'My Workflow'"),
    )

    description = models.TextField(
        blank=True,
        default="",
        max_length=5000,
        help_text=_(
            "Short description of what this workflow validates. "
            "Shown to authenticated users in the web UI, CLI, and API. "
            "For a longer public-facing description, use the workflow's "
            "public info page instead."
        ),
    )

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=_("Unique identifier for the workflow."),
    )

    slug = models.SlugField(
        null=False,
        blank=True,
        help_text=_(
            "A unique identifier for the workflow, used in URLs. "
            "(Leave blank to auto-generate from name.)",
        ),
    )

    allow_submission_name = models.BooleanField(
        default=True,
        help_text=_(
            "Allow users to submit a custom name along with their data for validation.",
        ),
    )

    allow_submission_meta_data = models.BooleanField(
        default=False,
        help_text=_(
            "Allow users to submit meta-data along with their data for validation.",
        ),
    )

    allow_submission_short_description = models.BooleanField(
        default=False,
        help_text=_(
            "Allow users to submit a short description along with "
            "their data for validation.",
        ),
    )
    version = models.CharField(
        max_length=40,
        blank=True,
        default="",
        validators=[validate_workflow_version],
        help_text=_(
            "Version identifier (integer or semantic version, e.g., '1' or '1.0.0')."
        ),
    )

    is_locked = models.BooleanField(
        default=False,
    )

    is_active = models.BooleanField(
        default=True,
        help_text=_("Inactive workflows stay visible but cannot run validations."),
    )
    is_archived = models.BooleanField(
        default=False,
        help_text=_(
            "Archived workflows are disabled and hidden unless explicitly shown."
        ),
    )
    is_tombstoned = models.BooleanField(
        default=False,
        help_text=_(
            "Tombstoned workflows are removed from normal product surfaces but "
            "kept for historical run and credential continuity."
        ),
    )
    tombstoned_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the workflow entered the break-glass tombstone state."),
    )
    tombstoned_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tombstoned_workflows",
        help_text=_("The user who initiated the break-glass tombstone flow."),
    )
    tombstone_reason = models.TextField(
        blank=True,
        default="",
        help_text=_("Human-entered reason recorded during break-glass delete."),
    )
    tombstone_workflow_definition_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_("Workflow definition digest captured at the time of tombstoning."),
    )

    make_info_page_public = models.BooleanField(
        default=False,
        help_text=_(
            "Allows non-logged in users to see the workflow's info page.",
        ),
    )

    is_public = models.BooleanField(
        default=False,
        help_text=_(
            "If true, any authenticated user can launch this workflow.",
        ),
    )

    # ── Agent (MCP) access ──────────────────────────────────────────────
    # These fields control whether AI agents can discover and invoke this
    # workflow via MCP. They are dormant in the community edition — the
    # cloud layer (or a self-hosted MCP server) reads them via the REST API.

    agent_access_enabled = models.BooleanField(
        default=False,
        help_text=_(
            "Master switch for all agent access via MCP. When enabled, "
            "authenticated agents in your organization can discover and "
            "invoke this workflow. For public cross-org discovery, also "
            "enable 'Public discovery'.",
        ),
    )

    agent_public_discovery = models.BooleanField(
        default=False,
        help_text=_(
            "List this workflow on the cross-org public catalog so agents "
            "outside your organization can discover and run it via x402 "
            "micropayments. Requires 'Agent access enabled' and "
            "automatically sets billing to 'Agent pays via x402'.",
        ),
    )

    agent_max_launches_per_hour = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Maximum agent launches per hour per wallet. "
            "Null means use the platform default.",
        ),
    )

    agent_billing_mode = models.CharField(
        max_length=30,
        choices=AgentBillingMode.choices,
        default=AgentBillingMode.AUTHOR_PAYS,
        help_text=_(
            "Who pays when an agent invokes this workflow. "
            "AUTHOR_PAYS uses your plan quota (authenticated agents only). "
            "AGENT_PAYS_X402 requires agents to pay per call via x402.",
        ),
    )

    agent_price_cents = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "Price per agent invocation in US cents (USDC equivalent). "
            "Required when billing mode is AGENT_PAYS_X402.",
        ),
    )

    featured_image_alt_candidates = ("name",)

    def __str__(self) -> str:  # pragma: no cover - display helper
        version = f" v{self.version}" if self.version else ""
        return f"{self.name}{version}"

    allowed_file_types = ArrayField(
        base_field=models.CharField(
            max_length=32,
            choices=SubmissionFileType.choices,
        ),
        default=_default_workflow_file_types,
        help_text=_(
            "Logical file types (JSON, XML, text, etc.) this workflow can accept.",
        ),
    )

    # Input retention: how long to keep user-uploaded input files.
    # Mirrors ``output_retention`` below — both fields carry the
    # workflow author's retention choice for one of the two data
    # streams a run touches. Renamed from ``input_retention`` in
    # the trust ADR's Phase 4 closing housekeeping (the old name
    # was ambiguous: "data" could mean input or output).
    input_retention = models.CharField(
        max_length=32,
        choices=SubmissionRetention.choices,
        default=SubmissionRetention.DO_NOT_STORE,
        help_text=_(
            "How long to keep user-submitted input files after validation "
            "completes. DO_NOT_STORE deletes the submission immediately "
            "after successful completion. The submission record is "
            "preserved for audit."
        ),
    )

    # Output retention: how long to keep validator results, artifacts, findings
    output_retention = models.CharField(
        max_length=32,
        choices=OutputRetention.choices,
        default=OutputRetention.STORE_30_DAYS,
        help_text=_(
            "How long to keep validation outputs (results, artifacts, findings) "
            "after validation completes. Users need time to download results, "
            "so immediate deletion is not an option."
        ),
    )

    success_message = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Custom message displayed when validation succeeds. "
            "Leave blank for the default message."
        ),
    )

    # ── Input contract ───────────────────────────────────────────────────
    # Author-declared JSON Schema defining the expected submission shape.
    # When set on a JSON-only workflow, the launch form shows a structured
    # form as an additional input mode.

    input_schema = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text=_(
            "JSON Schema defining the expected submission shape. "
            "When set on a JSON-only workflow, the launch form shows "
            "a structured form as an additional input mode."
        ),
    )

    input_schema_source_mode = models.CharField(
        max_length=32,
        choices=[
            ("json_schema", "JSON Schema"),
            ("pydantic", "Pydantic"),
        ],
        blank=True,
        default="",
        help_text=_(
            "How the workflow author last edited the input contract. "
            "Authoring metadata only; not part of the runtime contract."
        ),
    )

    input_schema_source_text = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Original authoring text for the input contract. "
            "Used to repopulate the workflow settings form."
        ),
    )

    # Methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": _("Name is required.")})
        if self.project_id and self.org_id and self.project.org_id != self.org_id:
            raise ValidationError(
                {"project": _("Project must belong to the workflow's organization.")},
            )
        allowed = [value for value in (self.allowed_file_types or []) if value]
        if not allowed:
            raise ValidationError(
                {
                    "allowed_file_types": _(
                        "Select at least one submission file type.",
                    ),
                },
            )
        normalized: list[str] = []
        for value in allowed:
            if value not in SubmissionFileType.values:
                raise ValidationError(
                    {
                        "allowed_file_types": _(
                            "'%(value)s' is not a supported submission file type.",
                        )
                        % {"value": value},
                    },
                )
            if value not in normalized:
                normalized.append(value)
        self.allowed_file_types = normalized

        # Validate input_schema against the supported v1 subset so that
        # direct saves outside WorkflowForm cannot persist contracts the
        # runtime adapters would silently ignore.
        if self.input_schema:
            # Input contracts require the workflow to accept only JSON
            # submissions — other file types have no structured schema.
            if set(normalized) != {SubmissionFileType.JSON}:
                raise ValidationError(
                    {
                        "input_schema": _(
                            "Input contracts are only supported when the "
                            "sole allowed file type is JSON."
                        ),
                    },
                )

            from validibot.workflows.schema_authoring import validate_schema_subset

            try:
                validate_schema_subset(self.input_schema)
            except ValidationError as exc:
                raise ValidationError(
                    {"input_schema": exc.messages},
                ) from exc

        # ── Cascade: disabling agent access clears public discovery ───
        if not self.agent_access_enabled:
            self.agent_public_discovery = False

        # ── Cascade: public discovery forces x402 billing ─────────────
        if self.agent_public_discovery:
            self.agent_billing_mode = AgentBillingMode.AGENT_PAYS_X402

        # ── Public discovery requires agent access (belt-and-suspenders)
        if self.agent_public_discovery and not self.agent_access_enabled:
            raise ValidationError(
                {
                    "agent_public_discovery": _(
                        "Public discovery requires agent access to be enabled first.",
                    ),
                },
            )

        # ── x402 billing requires a price ─────────────────────────────
        if (
            self.agent_billing_mode == AgentBillingMode.AGENT_PAYS_X402
            and not self.agent_price_cents
        ):
            raise ValidationError(
                {
                    "agent_price_cents": _(
                        "A price per invocation is required when agents pay "
                        "via x402 micropayments.",
                    ),
                },
            )

        # ── x402 billing requires DO_NOT_STORE data retention ──────────
        # x402 is anonymous per-call micropayment access for agents.
        # Storing agent submissions would undermine the privacy model
        # that x402 enables, so retention MUST be DO_NOT_STORE.  Enforced
        # on the model (not just the form) so the API and admin can't
        # bypass it.
        if (
            self.agent_billing_mode == AgentBillingMode.AGENT_PAYS_X402
            and self.input_retention != SubmissionRetention.DO_NOT_STORE
        ):
            raise ValidationError(
                {
                    "input_retention": _(
                        "Data retention must be 'Do not store' when agents "
                        "pay via x402 micropayments — x402 is anonymous "
                        "per-call access and storing submissions is "
                        "incompatible with its privacy model.",
                    ),
                },
            )

    def save(self, *args, **kwargs):
        # Auto-generate slug BEFORE validation so uniqueness checks work correctly
        if not self.slug:
            candidate = slugify(self.name) if self.name else ""
            if not candidate:
                # Fallback for names that don't slugify (e.g., only punctuation)
                candidate = f"wf-{uuid.uuid4().hex[:8]}"
            self.slug = candidate

        # Public workflows must have their info page public too
        if self.is_public and not self.make_info_page_public:
            self.make_info_page_public = True

        self.full_clean()
        super().save(*args, **kwargs)

    def can_view(self, *, user: User) -> bool:
        """
        Check if the given user can view this workflow.

        Access is granted if either:
        - User has WORKFLOW_VIEW permission in the workflow's org (org member), OR
        - User has an active WorkflowAccessGrant for ANY version of this
          workflow's family (guest, family-grant expansion)
        """
        if not user or not user.is_authenticated:
            return False

        # Org member check (existing behavior)
        if user.has_perm(PermissionCode.WORKFLOW_VIEW.value, self):
            return True

        # Guest grant check.
        #
        # Trust ADR-2026-04-27 + 2026-05-03 review (P1 #1): a grant on
        # ANY version of this workflow's family (same ``(org_id, slug)``
        # pair) authorises view of every version, matching the
        # visibility resolver's semantics in
        # :meth:`WorkflowQuerySet.for_user` (lines 146-153).
        #
        # Without this expansion, ``Workflow.objects.for_user(user)``
        # surfaces v2 in the catalog (correct), then ``can_view()``
        # rejects v2 on the detail page when only v1 has a grant —
        # because the FK reverse manager ``self.access_grants`` matches
        # the exact row only. Visibility and per-row checks must agree.
        return WorkflowAccessGrant.objects.filter(
            user=user,
            is_active=True,
            workflow__org_id=self.org_id,
            workflow__slug=self.slug,
        ).exists()

    def can_delete(self, *, user: User) -> bool:
        """
        Check if the given user can delete this workflow.
        Requires the ``workflow_edit`` permission in the workflow's org.
        """
        if not user or not user.is_authenticated:
            return False

        return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, self)

    def can_execute(self, *, user: User) -> bool:
        """
        Check if the given user can execute (run) this workflow.

        Access is granted if any of these conditions are met:
        - Workflow is public (any authenticated user)
        - User has WORKFLOW_LAUNCH permission in the workflow's org (org member)
        - User has an active WorkflowAccessGrant for ANY version of this
          workflow's family (guest, family-grant expansion)

        The workflow must also be active for execution to be allowed.
        """
        if not self.is_active or self.is_tombstoned:
            return False
        if not user or not user.is_authenticated:
            return False

        # Public workflows: any authenticated user can execute
        if self.is_public:
            return True

        # Org member check
        if user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, self):
            return True

        # Guest grant check.
        #
        # Trust ADR-2026-04-27 + 2026-05-03 review (P1 #1): family-grant
        # expansion. A grant on ANY version of this workflow's family
        # (same ``(org_id, slug)`` pair) authorises execution of every
        # version, matching the visibility resolver's semantics in
        # :meth:`WorkflowQuerySet.for_user` (lines 146-153).
        #
        # Without this expansion: a guest granted v1 sees v2 in their
        # catalog (the queryset expands by family), clicks Launch on v2,
        # and gets "permission denied" here — because ``self.access_grants``
        # only matches the exact row. The colleague's review flagged
        # the same divergence in :meth:`can_view`. Tightening one
        # without the other would just shift the bug from "deny on
        # launch" to "show in catalog -> 404 on click".
        return WorkflowAccessGrant.objects.filter(
            user=user,
            is_active=True,
            workflow__org_id=self.org_id,
            workflow__slug=self.slug,
        ).exists()

    def can_edit(self, *, user: User) -> bool:
        """
        Check if the given user can edit this workflow.
        Requires the ``workflow_edit`` permission in the workflow's organization.
        """
        if self.is_tombstoned or not user or not user.is_authenticated:
            return False

        return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, self)

    @transaction.atomic
    def tombstone(
        self,
        *,
        deleted_by: User | None,
        reason: str,
        workflow_definition_hash: str = "",
    ) -> None:
        """Place the workflow into the break-glass tombstone state.

        Tombstoning is intentionally stronger than ordinary archiving. It
        removes the workflow from normal listing, launch, sharing, and public
        surfaces while preserving the row so historical runs and issued
        credentials still have a stable target.
        """
        now = timezone.now()
        cleaned_reason = (reason or "").strip()
        self.is_tombstoned = True
        self.is_archived = True
        self.is_active = False
        self.is_public = False
        self.make_info_page_public = False
        self.agent_access_enabled = False
        self.agent_public_discovery = False
        self.tombstoned_at = now
        self.tombstoned_by = deleted_by
        self.tombstone_reason = cleaned_reason
        self.tombstone_workflow_definition_hash = workflow_definition_hash or ""
        self.save(
            update_fields=[
                "is_tombstoned",
                "is_archived",
                "is_active",
                "is_public",
                "make_info_page_public",
                "agent_access_enabled",
                "agent_public_discovery",
                "tombstoned_at",
                "tombstoned_by",
                "tombstone_reason",
                "tombstone_workflow_definition_hash",
                "modified",
            ],
        )
        self.access_grants.filter(is_active=True).update(
            is_active=False,
            modified=now,
        )
        self.invites.filter(status=InviteStatus.PENDING).update(
            status=InviteStatus.CANCELED,
            modified=now,
        )

    @property
    def workflow_type(self) -> str:
        """
        Classification for metering: BASIC or ADVANCED.

        A workflow is ADVANCED if any of its validator steps uses a
        HIGH-compute validator. Used by the cloud billing system to
        determine whether a run consumes basic launch quota or compute
        credits.
        """
        from validibot.validations.constants import ComputeTier

        has_high_compute = self.steps.filter(
            validator__compute_tier=ComputeTier.HIGH,
        ).exists()
        return "ADVANCED" if has_high_compute else "BASIC"

    def allowed_file_type_labels(self) -> list[str]:
        labels: list[str] = []
        for value in self.allowed_file_types or []:
            try:
                labels.append(str(SubmissionFileType(value).label))
            except Exception:
                labels.append(str(value))
        return labels

    def supports_file_type(self, file_type: str) -> bool:
        normalized = (file_type or "").lower()
        return normalized in {ft.lower() for ft in (self.allowed_file_types or [])}

    def validator_is_compatible(self, validator) -> bool:
        if not validator:
            return True
        validator_types = set(
            getattr(validator, "supported_file_types", []) or [],
        )
        workflow_types = set(self.allowed_file_types or [])
        return bool(
            {ft.lower() for ft in workflow_types}
            & {ft.lower() for ft in validator_types}
        )

    def first_incompatible_step(self, file_type: str):
        if not file_type:
            return None
        normalized = file_type.lower()
        steps = self.steps.select_related("validator").all()
        for step in steps:
            validator = step.validator
            if validator and hasattr(validator, "supports_file_type"):
                if not validator.supports_file_type(normalized):
                    return step
        return None

    def has_runs(self) -> bool:
        """Return True if any validation run targets this workflow.

        A workflow with runs is "in use" — its launch contract is
        the rules that those runs ran under, and any contract edit
        from this point should produce a new version rather than
        silently mutating in place.

        See :meth:`requires_new_version_for_contract_edits`.
        """
        return self.validation_runs.exists()

    def requires_new_version_for_contract_edits(self) -> bool:
        """Return True if contract-field edits require a new version.

        Contract fields are listed in
        :data:`validibot.workflows.services.versioning.CONTRACT_FIELDS`
        — they're the fields that change *what* a workflow does
        when launched (allowed file types, retention, agent
        publication state, etc.). Non-contract fields (name,
        description, lifecycle flags) can always be edited in place.

        Once a workflow is locked OR has runs, contract-field
        edits should be rejected by forms / API serializers, and the
        operator should be directed to clone the workflow to a new
        version using
        :meth:`WorkflowVersioningService.clone`.
        """
        return self.is_locked or self.has_runs()

    def changed_contract_fields(self, proposed: dict) -> set[str]:
        """Return contract-field names whose proposed value differs from current.

        Pure helper for forms/serializers/scripts that need to detect
        in-place contract edits before deciding whether to allow them.

        The comparison is done against the *current* in-memory values
        on this instance — callers that want to compare against the
        database row should refresh first (``self.refresh_from_db()``).
        Forms get this for free because Django's ``ModelForm.clean()``
        runs before ``_post_clean()`` merges ``cleaned_data`` into the
        instance, so at that point ``self.instance`` still carries the
        DB values.

        Only fields *present* in ``proposed`` are considered — a caller
        editing a subset of contract fields (e.g. only ``input_retention``)
        does not need to pass every contract field. ``ArrayField`` values
        like ``allowed_file_types`` are compared as sets so re-ordering
        without a real change isn't flagged.

        Args:
            proposed: A dict of ``{field_name: proposed_value}``.
                Typically this is ``cleaned_data`` from a ModelForm or
                ``serializer.validated_data`` from a DRF serializer.

        Returns:
            The subset of contract field names whose proposed value is
            different from the current value. Empty set if everything
            matches (or if no contract fields appear in ``proposed``).
        """
        # Local import: services.versioning imports models, models can't
        # import services at module-load time without circularity.
        from validibot.workflows.services.versioning import CONTRACT_FIELDS

        changed: set[str] = set()
        for field_name in CONTRACT_FIELDS:
            if field_name not in proposed:
                continue
            current = getattr(self, field_name, None)
            new_value = proposed[field_name]
            # Order-insensitive comparison for list-shaped fields like
            # allowed_file_types — reordering checkboxes shouldn't count
            # as a contract change.
            if isinstance(current, list) and isinstance(new_value, (list, tuple)):
                if set(current) != set(new_value):
                    changed.add(field_name)
            elif current != new_value:
                changed.add(field_name)
        return changed

    @transaction.atomic
    def clone_to_new_version(self, user) -> Workflow:
        """
        Create an identical workflow with version+1 and copied steps.
        Locks old version.

        Phase 3 of ADR-2026-04-27: this method now delegates to
        :class:`validibot.workflows.services.versioning.WorkflowVersioningService`
        so the clone copies the **complete** workflow contract
        (steps + step resources + public info + role access + signal
        mappings), not just steps and resources. The service returns
        a structured :class:`CloneReport` that callers can inspect;
        for backward compatibility this method continues to return
        just the new ``Workflow`` row.
        """
        # Local import to avoid circular dependency at module load.
        from validibot.workflows.services.versioning import WorkflowVersioningService

        report = WorkflowVersioningService.clone(self, user=user)
        return Workflow.objects.get(pk=report.new_workflow_id)

    def _determine_next_version_label(self, versions) -> str:
        """
        Return a simple integer version label for the cloned workflow.

        Parses existing versions (integer or semver) and returns the next
        major version as an integer string. This ensures cloned versions
        are always valid integers (e.g., "1", "2", "3") and never produce
        invalid versions like "2.5".
        """
        max_major = 0
        for raw in versions:
            if raw is None:
                continue
            candidate = str(raw).strip()
            if not candidate:
                continue

            # Parse as integer
            if candidate.isdigit():
                max_major = max(max_major, int(candidate))
                continue

            # Parse as semver - extract major version
            match = SEMVER_PATTERN.match(candidate)
            if match:
                major = int(match.group("major"))
                max_major = max(max_major, major)

        # Return next integer version
        return str(max_major + 1)

    @property
    def has_signed_credential_action(self) -> bool:
        """Whether any step in this workflow issues a signed credential."""
        from validibot.actions.constants import CredentialActionType

        return self.steps.filter(
            action__definition__type=CredentialActionType.SIGNED_CREDENTIAL,
        ).exists()

    @property
    def is_advanced(self) -> bool:
        """
        Check if this workflow uses any advanced (high-compute) validators.
        Returns:
            True if any step uses an advanced validator type.
        """
        from validibot.validations.constants import ADVANCED_VALIDATION_TYPES

        return self.steps.filter(
            validator__validation_type__in=ADVANCED_VALIDATION_TYPES,
        ).exists()

    @property
    def get_public_info(self) -> WorkflowPublicInfo:
        public_info, _ = WorkflowPublicInfo.objects.get_or_create(workflow=self)
        return public_info


class WorkflowPublicInfo(TimeStampedModel):
    workflow = models.OneToOneField(
        Workflow,
        on_delete=models.CASCADE,
        related_name="public_info",
    )
    title = models.CharField(
        max_length=200,
        default="",
        help_text=_(
            "Optional title to show on the public info page. "
            "If blank, the Workflow name will be used.",
        ),
    )
    content_md = models.TextField()  # user-authored Markdown
    content_html = models.TextField(editable=False)  # cached sanitized HTML

    show_steps = models.BooleanField(
        default=True,
        help_text=_("Whether to show the workflow steps on the public info page."),
    )

    def __str__(self):
        return f"Public info for {self.workflow}"

    def save(self, *args, **kwargs):
        self.compile_content()
        super().save(*args, **kwargs)

    def compile_content(self):
        try:
            self.content_html = render_markdown_safe(self.content_md)
        except Exception:
            logger.exception("Error rendering markdown for workflow public info")
            self.content_html = ""

    def get_title(self) -> str:
        if self.title and self.title.strip():
            return self.title.strip()
        return self.workflow.name

    def get_html_content(self) -> str:
        return self.content_html or ""


class WorkflowStep(TimeStampedModel):
    """
    Ordered unit of work within a workflow.

    Each step is either a validator execution or an action (never both). Validator
    steps may optionally link a `Ruleset` to override the validator's default
    assertions; action steps skip rulesets and instead reference a concrete
    `Action` subclass (Slack message, signed credential, etc.) that performs a
    side effect. `config` stores per-step JSON tweaks consumed by the validator or
    action at runtime such as severity thresholds or templated text.
    """

    class Meta:
        unique_together = [
            (
                "workflow",
                "order",
            ),
        ]
        ordering = ["order"]
        constraints = [
            models.CheckConstraint(
                name="workflowstep_validator_xor_action",
                condition=(
                    Q(validator__isnull=False, action__isnull=True)
                    | Q(validator__isnull=True, action__isnull=False)
                ),
            ),
            # step_key is the stable namespace for cross-step signal
            # references in CEL and APIs. Must be unique within a workflow.
            models.UniqueConstraint(
                fields=["workflow", "step_key"],
                condition=~Q(step_key=""),
                name="uq_workflowstep_workflow_step_key",
            ),
        ]

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="steps",
    )

    order = models.PositiveIntegerField()  # 10,20,30... leave gaps for inserts

    name = models.CharField(
        max_length=200,
        blank=True,
        default="",
    )

    step_key = models.SlugField(
        max_length=100,
        blank=True,
        default="",
        help_text=_(
            "Stable identifier for this step within the workflow. "
            "Used to reference output data from this step in "
            "downstream assertions (e.g., "
            "steps.simulation.signals.site_eui_kwh_m2). "
            "Auto-generated from the step name on creation. "
            "Immutable once set."
        ),
    )

    description = models.CharField(
        max_length=2000,
        blank=True,
        default="",
        help_text=_("Brief description to help users understand what this step does."),
    )
    notes = models.CharField(
        max_length=2000,
        blank=True,
        default="",
        help_text=_(
            "Author notes about this step (visible only by you and other users "
            "with author permissions for this workflow).",
        ),
    )
    display_schema = models.BooleanField(
        default=False,
        help_text=_("Allow launchers to view this schema in public workflow pages."),
    )
    show_success_messages = models.BooleanField(
        default=False,
        help_text=_(
            "When enabled, assertions display their success message when they pass. "
            "If an assertion has no success message defined, a default is generated."
        ),
    )

    validator = models.ForeignKey(
        "validations.Validator",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )

    action = models.ForeignKey(
        Action,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="workflow_steps",
    )

    ruleset = models.ForeignKey(
        "validations.Ruleset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    # Optional per-step config (e.g., severity thresholds, mapping).
    # Use the typed_config property for type-safe access — see step_configs.py.
    config = models.JSONField(default=dict, blank=True)

    @property
    def typed_config(self):
        """Return this step's config as a typed Pydantic model.

        Parses the raw config JSONField into the appropriate Pydantic model
        based on the step's validator or action type. For example, an
        EnergyPlus step returns an EnergyPlusStepConfig instance with typed
        fields like ``idf_checks: list[str]``.

        Resource file references (weather files, templates) are stored
        relationally via ``self.step_resources`` rather than in config.

        Returns BaseStepConfig for unknown types (still gives dict-like access
        via ``extra="allow"``).

        See Also:
            validibot.workflows.step_configs for available config models.
            WorkflowStepResource for relational resource bindings.
        """
        from validibot.workflows.step_configs import get_step_config

        return get_step_config(self)

    @property
    def step_number(self) -> int:
        """Return the display position for this step based on its order."""
        if not self.order:
            return 1
        return max(1, math.ceil(self.order / 10))

    @property
    def step_number_display(self) -> str:
        """Return a localized display string for this step's number."""
        step_number = self.step_number
        return _("Step") + f" {step_number}"

    def save(self, *args, **kwargs):
        """Auto-generate step_key on first save; prevent mutation after.

        The step_key is the stable workflow contract identifier used for
        cross-step signal references in CEL and APIs. It must not change
        after creation because assertions and API consumers may reference
        it. Auto-generated from the step name via slugify() if not set.
        """
        from slugify import slugify

        is_new = self._state.adding

        if is_new and not self.step_key and self.name:
            # Auto-generate from name, ensuring uniqueness within workflow
            base_key = slugify(self.name, separator="_") or "step"
            candidate = base_key
            counter = 2
            while (
                WorkflowStep.objects.filter(
                    workflow=self.workflow,
                    step_key=candidate,
                )
                .exclude(pk=self.pk)
                .exists()
            ):
                candidate = f"{base_key}_{counter}"
                counter += 1
            self.step_key = candidate

        if not is_new and self.pk:
            # Prevent mutation of step_key after initial creation
            try:
                existing = WorkflowStep.objects.values_list(
                    "step_key",
                    flat=True,
                ).get(pk=self.pk)
            except WorkflowStep.DoesNotExist:
                existing = ""

            if existing and self.step_key != existing:
                self.step_key = existing

        super().save(*args, **kwargs)

    def clean(self):
        super().clean()

        if (
            WorkflowStep.objects.filter(workflow=self.workflow, order=self.order)
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError({"order": _("Order already used in this workflow.")})

        # Ensure the ruleset chosen matches the validator's type
        if bool(self.validator_id) == bool(self.action_id):
            raise ValidationError(
                {
                    "validator": _(
                        "Specify either a validator or an action for this step.",
                    ),
                    "action": _(
                        "Specify either a validator or an action for this step.",
                    ),
                },
            )

        if (
            self.validator
            and self.ruleset
            and (self.ruleset.ruleset_type != self.validator.validation_type)
        ):
            raise ValidationError(
                {
                    "ruleset": _("Ruleset type must match validator type."),
                },
            )

        if self.action and self.display_schema:
            self.display_schema = False

        # ── Credential step placement rules ──
        # At most one SignedCredentialAction per workflow, and it must
        # come after all blocking steps.
        if self.action_id:
            self._validate_credential_step_placement()

        # Validate config against the typed Pydantic model for this step type.
        # This catches typos and type mismatches at save time rather than at
        # runtime during validation execution.
        if self.config:
            from pydantic import ValidationError as PydanticValidationError

            from validibot.workflows.step_configs import get_step_config

            try:
                get_step_config(self)
            except PydanticValidationError as exc:
                raise ValidationError(
                    {"config": str(exc)},
                ) from exc

    def _validate_credential_step_placement(self):
        """Enforce credential step uniqueness and ordering rules.

        Rules:
            - At most one SignedCredentialAction step per workflow.
            - The credential step must come after all validator steps
              and all BLOCKING action steps.
            - ADVISORY action steps may appear after the credential step.

        This method is a no-op for non-credential action steps.
        """
        from validibot.actions.constants import ActionFailureMode
        from validibot.actions.constants import CredentialActionType

        # Only apply to signed credential actions.
        action = self.action
        if not action or not action.definition_id:
            return
        if action.definition.type != CredentialActionType.SIGNED_CREDENTIAL:
            return

        # Rule 1: At most one credential step per workflow.
        existing_credential_steps = WorkflowStep.objects.filter(
            workflow=self.workflow,
            action__definition__type=CredentialActionType.SIGNED_CREDENTIAL,
        ).exclude(pk=self.pk)
        if existing_credential_steps.exists():
            raise ValidationError(
                {
                    "action": _(
                        "A workflow can have at most one signed credential step."
                    ),
                },
            )

        # Rule 2: No validator steps may appear after the credential
        # step.  The ADR says: "the credential step must come after
        # all blocking work whose failure would change the meaning of
        # the claim."  Validators are implicitly blocking.
        validators_after_us = WorkflowStep.objects.filter(
            workflow=self.workflow,
            order__gt=self.order,
            validator__isnull=False,
        ).exclude(pk=self.pk)

        if validators_after_us.exists():
            raise ValidationError(
                {
                    "order": _(
                        "The signed credential step must come after all "
                        "validation steps. Move it to the end of the "
                        "workflow, or move the validator steps before it."
                    ),
                },
            )

        # Rule 3: No BLOCKING action steps may appear after the
        # credential step.  ADVISORY actions after it are fine.
        blocking_actions_after_us = WorkflowStep.objects.filter(
            workflow=self.workflow,
            order__gt=self.order,
            action__isnull=False,
            action__failure_mode=ActionFailureMode.BLOCKING,
        ).exclude(pk=self.pk)

        if blocking_actions_after_us.exists():
            raise ValidationError(
                {
                    "order": _(
                        "The signed credential step must come after all "
                        "blocking action steps."
                    ),
                },
            )


class WorkflowStepResource(models.Model):
    """Links a resource to a workflow step.

    Supports two mutually exclusive modes:

    1. **Catalog reference** — points to a shared ValidatorResourceFile from the
       validator library (e.g., weather files). Uses PROTECT on delete so you
       can't delete a catalog file that steps still need.

    2. **Step-owned file** — stores the file directly on this record (e.g., a
       template IDF uploaded for this specific step). The file cascades
       with the step — no orphaned files, no catalog clutter.

    Exactly one of ``validator_resource_file`` or ``step_resource_file`` must be
    populated (DB-level XOR constraint).

    See Also:
        ADR 2026-03-04: EnergyPlus Parameterized Model Templates
        (WorkflowStepResource — Relational Resource Binding)
    """

    # Role constants — the purpose of this resource in the step.
    WEATHER_FILE = "WEATHER_FILE"
    MODEL_TEMPLATE = "MODEL_TEMPLATE"
    FMU_MODEL = "FMU_MODEL"

    step = models.ForeignKey(
        "workflows.WorkflowStep",
        on_delete=models.CASCADE,
        related_name="step_resources",
    )
    role = models.CharField(
        max_length=50,
        help_text="Purpose of this resource (e.g., WEATHER_FILE, MODEL_TEMPLATE).",
    )

    # ── Mode 1: Catalog reference (shared resources) ──────────────

    validator_resource_file = models.ForeignKey(
        "validations.ValidatorResourceFile",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="step_usages",
        help_text="Shared resource from the validator library.",
    )

    # ── Mode 2: Step-owned file (step-specific resources) ─────────

    step_resource_file = models.FileField(
        upload_to="step_resources/",
        max_length=500,
        blank=True,
        help_text="File owned by this step. Deleted when the step is deleted.",
    )
    filename = models.CharField(
        max_length=255,
        blank=True,
        help_text="Original filename of the step-owned file.",
    )
    resource_type = models.CharField(
        max_length=32,
        blank=True,
        help_text="Resource type identifier for step-owned files.",
    )

    # ADR-2026-04-27 Phase 3, task 11: SHA-256 of the step-owned
    # file's content (only meaningful in step-owned mode; catalog
    # references inherit the hash from their ValidatorResourceFile).
    # Auto-populated on save. Mutation gating happens at save time:
    # if this resource's step lives on a locked workflow and the
    # bytes change, save() raises.
    content_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "SHA-256 of step_resource_file content. Only used for "
            "step-owned files; catalog references look up the hash "
            "via validator_resource_file.content_hash."
        ),
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="ck_step_resource_xor_file",
                condition=(
                    models.Q(
                        validator_resource_file__isnull=False,
                        step_resource_file="",
                    )
                    | (
                        models.Q(validator_resource_file__isnull=True)
                        & ~models.Q(step_resource_file="")
                    )
                ),
            ),
        ]

    def __str__(self):
        if self.is_catalog_reference:
            return (
                f"StepResource({self.step_id}, {self.role},"
                f" catalog={self.validator_resource_file_id})"
            )
        return f"StepResource({self.step_id}, {self.role}, file={self.filename})"

    def save(self, *args, **kwargs):
        """Compute ``content_hash`` for step-owned files, gate drift on lock.

        Catalog references (``validator_resource_file`` set) are
        immutable links to another row — their hash story belongs to
        ``ValidatorResourceFile.content_hash``; we leave
        ``self.content_hash`` empty in that mode.

        For step-owned files, the on-save hash recompute is the
        choke point: a different hash than what's stored, AND a
        locked workflow on the other side, raises before persistence.
        """
        from validibot.core.filesafety import sha256_field_file

        if self.is_step_owned:
            new_hash = sha256_field_file(self.step_resource_file)
            if (
                self.pk
                and self.content_hash
                and new_hash != self.content_hash
                and self.is_used_by_locked_workflow()
            ):
                from django.core.exceptions import ValidationError

                raise ValidationError(
                    {
                        "step_resource_file": (
                            "This step-owned file is part of a locked "
                            "workflow's contract; its bytes cannot "
                            "change in place. Clone the workflow to a "
                            "new version to upload a different file."
                        ),
                    },
                )
            self.content_hash = new_hash
        else:
            # Catalog reference: leave hash blank, the source row owns it.
            self.content_hash = ""
        super().save(*args, **kwargs)

    @property
    def is_catalog_reference(self) -> bool:
        """True when this resource points to a shared ValidatorResourceFile."""
        return self.validator_resource_file_id is not None

    @property
    def is_step_owned(self) -> bool:
        """True when this resource stores a file directly on this record."""
        return self.validator_resource_file_id is None

    def is_used_by_locked_workflow(self) -> bool:
        """Return True if our step lives on a locked or used workflow.

        Step-owned resources are scoped to a single step (one row,
        one workflow). The check therefore walks ``step.workflow``
        and applies the standard locked-or-has-runs gate.
        """
        if not self.step_id:
            return False
        workflow = self.step.workflow
        return workflow.is_locked or workflow.has_runs()

    def get_storage_uri(self) -> str:
        """Return the storage URI for this resource, regardless of mode.

        For catalog references, delegates to the ValidatorResourceFile's
        ``get_storage_uri()`` method (which handles GCS vs local paths).
        For step-owned files, constructs a proper storage URI from the
        FileField's storage backend — ``gs://`` for GCS or ``file://``
        for local filesystems.

        This follows the same pattern as
        ``ValidatorResourceFile.get_storage_uri()``.
        """
        if self.is_catalog_reference:
            return self.validator_resource_file.get_storage_uri()

        # Step-owned file: construct proper URI for the storage backend.
        # Previously returned `.url` which gives a media URL (e.g.,
        # "/media/files/...") instead of a storage URI that containers
        # can use to download the file.
        file_storage = self.step_resource_file.storage
        storage_class_name = file_storage.__class__.__name__
        if storage_class_name == "GoogleCloudStorage":
            bucket_name = getattr(file_storage, "bucket_name", "")
            location = getattr(file_storage, "location", "")
            if location:
                return f"gs://{bucket_name}/{location}/{self.step_resource_file.name}"
            return f"gs://{bucket_name}/{self.step_resource_file.name}"

        # Local filesystem storage
        return f"file://{self.step_resource_file.path}"


# ── Storage cleanup for step-owned files ──────────────────────────────
#
# Django's FileField does NOT auto-delete files from storage when a model
# instance is deleted.  For step-owned files, the WorkflowStepResource
# row is the *only* reference to the file — without this signal, deleting
# the row (via cascade, template replacement, or explicit delete) leaves
# orphaned files in storage (local filesystem or GCS bucket).


def _cleanup_step_resource_file(sender, instance, **kwargs):
    """Delete the physical file from storage when a WorkflowStepResource is removed."""
    if instance.step_resource_file:
        try:
            instance.step_resource_file.delete(save=False)
        except Exception:
            logger.warning(
                "Failed to delete step-owned file for WorkflowStepResource %s",
                instance.pk,
                exc_info=True,
            )


models.signals.post_delete.connect(
    _cleanup_step_resource_file,
    sender=WorkflowStepResource,
)


class WorkflowRoleAccess(models.Model):
    """
    Grants access to a workflow to users holding specific roles in the workflow's org.
    Example: allow all 'ADMIN' or 'OWNER' members of the org.
    """

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="role_access",
    )

    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        related_name="workflow_role_access",
    )

    class Meta:
        unique_together = [("workflow", "role")]
        indexes = [models.Index(fields=["workflow", "role"])]

    def __str__(self):
        return f"{self.workflow_id}:{self.role}"


class WorkflowAccessGrant(TimeStampedModel):
    """
    Grants a user (typically external) access to a specific workflow without
    requiring org membership. Used for cross-organization workflow sharing.

    Workflow Guests are users who have access grants but no org membership.
    Their usage is billed/metered against the workflow owner's org.

    This is distinct from WorkflowRoleAccess which grants access based on
    org membership roles. WorkflowAccessGrant is for external users who
    are not members of the workflow's organization.
    """

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="access_grants",
        help_text=_("The workflow this grant provides access to."),
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="workflow_grants",
        help_text=_("The user who has been granted access."),
    )
    granted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_workflow_access",
        help_text=_("The user who created this grant."),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_("Whether this grant is currently active."),
    )
    notes = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional notes about this access grant."),
    )

    class Meta:
        unique_together = [("workflow", "user")]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["workflow", "is_active"]),
        ]
        verbose_name = _("workflow access grant")
        verbose_name_plural = _("workflow access grants")

    def __str__(self):
        return f"{self.user} -> {self.workflow.name}"

    def revoke(self) -> None:
        """Revoke this access grant."""
        self.is_active = False
        self.save(update_fields=["is_active", "modified"])


class WorkflowInvite(TimeStampedModel):
    """
    Invitation for an external user to access a specific workflow as a guest.

    Unlike MemberInvite (for org membership), accepting this invite creates a
    WorkflowAccessGrant but NOT a Membership. The invited user operates as a
    Workflow Guest without an organization context.

    Workflow invites enable cross-org sharing where:
    - The inviter is an author in the workflow's org
    - The invitee may or may not have an existing account
    - Upon acceptance, the invitee gets access to the specific workflow only
    - Usage is billed to the workflow owner's org
    """

    # Keep Status as alias for backward compatibility
    Status = InviteStatus

    # Default invite expiry: 7 days
    DEFAULT_EXPIRY_DAYS = 7

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="invites",
        help_text=_("The workflow this invite grants access to."),
    )
    inviter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sent_workflow_invites",
        help_text=_("The user who sent this invite."),
    )
    invitee_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="received_workflow_invites",
        null=True,
        blank=True,
        help_text=_("The invited user, if they already have an account."),
    )
    invitee_email = models.EmailField(
        blank=True,
        help_text=_("Email address of invitee (used when inviting non-users)."),
    )
    status = models.CharField(
        max_length=16,
        choices=InviteStatus.choices,
        default=InviteStatus.PENDING,
    )
    token = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=_("Unique token for invite acceptance URL."),
    )
    expires_at = models.DateTimeField(
        help_text=_("When this invite expires."),
    )

    class Meta:
        ordering = ["-created"]
        verbose_name = _("workflow invite")
        verbose_name_plural = _("workflow invites")
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["invitee_email", "status"]),
            models.Index(fields=["workflow", "status"]),
        ]

    def __str__(self):
        target = self.invitee_user or self.invitee_email
        return f"Invite to {self.workflow.name} for {target}"

    @classmethod
    def create_with_expiry(
        cls,
        *,
        workflow: Workflow,
        inviter: User,
        invitee_email: str,
        invitee_user: User | None = None,
        expiry_days: int | None = None,
        send_email: bool = True,
    ) -> WorkflowInvite:
        """
        Create a new workflow invite with default expiry.

        Args:
            workflow: The workflow to grant access to.
            inviter: The user sending the invite.
            invitee_email: Email of the person being invited.
            invitee_user: Optional existing user if email matches.
            expiry_days: Days until expiry (default: 7).
            send_email: Whether to send an invitation email (default: True).

        Returns:
            The created WorkflowInvite instance.
        """
        from datetime import timedelta

        from django.utils import timezone

        days = expiry_days or cls.DEFAULT_EXPIRY_DAYS
        expires_at = timezone.now() + timedelta(days=days)

        invite = cls.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email=invitee_email,
            invitee_user=invitee_user,
            expires_at=expires_at,
        )

        if send_email:
            from validibot.workflows.emails import send_workflow_invite_email

            send_workflow_invite_email(invite)

        return invite

    def mark_expired_if_needed(self) -> bool:
        """
        Check if invite has expired and update status if so.

        Returns:
            True if the invite was marked as expired, False otherwise.
        """
        from django.utils import timezone

        if self.status != InviteStatus.PENDING:
            return False

        if timezone.now() >= self.expires_at:
            self.status = InviteStatus.EXPIRED
            self.save(update_fields=["status", "modified"])
            return True

        return False

    def accept(self, user: User | None = None) -> WorkflowAccessGrant:
        """
        Accept this invite and create a WorkflowAccessGrant.

        Args:
            user: The user accepting the invite. If not provided, uses
                  invitee_user. Required if invitee_user is not set.

        Returns:
            The created WorkflowAccessGrant.

        Raises:
            ValueError: If invite is not in PENDING status or no user provided.
        """
        # Check for expiry first
        if self.mark_expired_if_needed():
            raise ValueError("Invite has expired")

        if self.status != InviteStatus.PENDING:
            msg = f"Cannot accept invite with status {self.status}"
            raise ValueError(msg)

        accepting_user = user or self.invitee_user
        if not accepting_user:
            msg = "No user provided to accept invite"
            raise ValueError(msg)

        # Create the access grant
        grant, _created = WorkflowAccessGrant.objects.get_or_create(
            workflow=self.workflow,
            user=accepting_user,
            defaults={
                "granted_by": self.inviter,
                "is_active": True,
            },
        )

        # If grant already existed but was inactive, reactivate it
        if not _created and not grant.is_active:
            grant.is_active = True
            grant.granted_by = self.inviter
            grant.save(update_fields=["is_active", "granted_by", "modified"])

        # Update invite status
        self.status = InviteStatus.ACCEPTED
        if not self.invitee_user:
            self.invitee_user = accepting_user
        self.save(update_fields=["status", "invitee_user", "modified"])

        return grant

    def decline(self) -> None:
        """Decline this invite."""
        if self.status != InviteStatus.PENDING:
            return
        self.status = InviteStatus.DECLINED
        self.save(update_fields=["status", "modified"])

    def cancel(self) -> None:
        """Cancel this invite (called by the inviter)."""
        if self.status != InviteStatus.PENDING:
            return
        self.status = InviteStatus.CANCELED
        self.save(update_fields=["status", "modified"])

    @property
    def is_expired(self) -> bool:
        """Check if invite has expired without updating status."""
        from django.utils import timezone

        return self.status == InviteStatus.EXPIRED or timezone.now() >= self.expires_at

    @property
    def is_pending(self) -> bool:
        """Check if invite is still pending and not expired."""
        return self.status == InviteStatus.PENDING and not self.is_expired


class GuestInvite(TimeStampedModel):
    """
    Invitation for an external user to access multiple workflows in an org as a guest.

    Unlike WorkflowInvite (for a single workflow), this invite grants access to either:
    - All current workflows in the org (scope=ALL), or
    - A selected subset of workflows (scope=SELECTED)

    When accepted, the invite expands into individual WorkflowAccessGrant rows
    for each workflow in the resolved set. New workflows created after acceptance
    are NOT automatically shared.

    This model enables the org-level guest management UI where admins can invite
    guests to multiple workflows at once.
    """

    # Keep Status as alias for backward compatibility
    Status = InviteStatus

    class Scope(models.TextChoices):
        ALL = "ALL", _("All workflows in org")
        SELECTED = "SELECTED", _("Selected workflows")

    # Default invite expiry: 7 days
    DEFAULT_EXPIRY_DAYS = 7

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    org = models.ForeignKey(
        "users.Organization",
        on_delete=models.CASCADE,
        related_name="guest_invites",
        help_text=_("The organization this invite is for."),
    )
    inviter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sent_guest_invites",
        help_text=_("The user who sent this invite."),
    )
    invitee_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="received_guest_invites",
        null=True,
        blank=True,
        help_text=_("The invited user, if they already have an account."),
    )
    invitee_email = models.EmailField(
        blank=True,
        help_text=_("Email address of invitee (used when inviting non-users)."),
    )
    scope = models.CharField(
        max_length=16,
        choices=Scope.choices,
        default=Scope.SELECTED,
        help_text=_("Whether to grant access to all current workflows or a selection."),
    )
    workflows = models.ManyToManyField(
        Workflow,
        blank=True,
        related_name="guest_invites",
        help_text=_("Workflows to grant access to (used when scope=SELECTED)."),
    )
    status = models.CharField(
        max_length=16,
        choices=InviteStatus.choices,
        default=InviteStatus.PENDING,
    )
    token = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=_("Unique token for invite acceptance URL."),
    )
    expires_at = models.DateTimeField(
        help_text=_("When this invite expires."),
    )

    class Meta:
        ordering = ["-created"]
        verbose_name = _("guest invite")
        verbose_name_plural = _("guest invites")
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["invitee_email", "status"]),
            models.Index(fields=["org", "status"]),
        ]

    def __str__(self):
        target = self.invitee_user or self.invitee_email
        return f"Guest invite to {self.org.name} for {target}"

    def get_resolved_workflows(self) -> models.QuerySet[Workflow]:
        """
        Get the workflows this invite grants access to.

        For scope=ALL, returns all active, non-archived, non-tombstoned
        workflows in the org.
        For scope=SELECTED, returns the explicitly selected workflows.
        """
        if self.scope == self.Scope.ALL:
            return Workflow.objects.filter(
                org=self.org,
                is_active=True,
                is_archived=False,
                is_tombstoned=False,
            )
        return self.workflows.filter(
            is_active=True,
            is_archived=False,
            is_tombstoned=False,
        )

    @classmethod
    def create_with_expiry(
        cls,
        *,
        org,
        inviter: User,
        invitee_email: str,
        scope: str,
        workflows: list | None = None,
        invitee_user: User | None = None,
        expiry_days: int | None = None,
        send_email: bool = True,
    ) -> GuestInvite:
        """
        Create a new guest invite with default expiry.

        Args:
            org: The organization to grant guest access to.
            inviter: The user sending the invite.
            invitee_email: Email of the person being invited.
            scope: Either 'ALL' or 'SELECTED'.
            workflows: List of workflows (required if scope=SELECTED).
            invitee_user: Optional existing user if email matches.
            expiry_days: Days until expiry (default: 7).
            send_email: Whether to send an invitation email (default: True).

        Returns:
            The created GuestInvite instance.
        """
        from datetime import timedelta

        from django.utils import timezone

        days = expiry_days or cls.DEFAULT_EXPIRY_DAYS
        expires_at = timezone.now() + timedelta(days=days)

        invite = cls.objects.create(
            org=org,
            inviter=inviter,
            invitee_email=invitee_email,
            invitee_user=invitee_user,
            scope=scope,
            expires_at=expires_at,
        )

        if scope == cls.Scope.SELECTED and workflows:
            invite.workflows.set(workflows)

        if send_email:
            from validibot.workflows.emails import send_guest_invite_email

            send_guest_invite_email(invite)

        return invite

    def mark_expired_if_needed(self) -> bool:
        """
        Check if invite has expired and update status if so.

        Returns:
            True if the invite was marked as expired, False otherwise.
        """
        from django.utils import timezone

        if self.status != InviteStatus.PENDING:
            return False

        if timezone.now() >= self.expires_at:
            self.status = InviteStatus.EXPIRED
            self.save(update_fields=["status", "modified"])
            return True

        return False

    def accept(self, user: User | None = None) -> list[WorkflowAccessGrant]:
        """
        Accept this invite and create WorkflowAccessGrants for all resolved workflows.

        Args:
            user: The user accepting the invite. If not provided, uses
                  invitee_user. Required if invitee_user is not set.

        Returns:
            List of created/updated WorkflowAccessGrant instances.

        Raises:
            ValueError: If invite is not in PENDING status or no user provided.
        """
        # Check for expiry first
        if self.mark_expired_if_needed():
            raise ValueError("Invite has expired")

        if self.status != InviteStatus.PENDING:
            msg = f"Cannot accept invite with status {self.status}"
            raise ValueError(msg)

        accepting_user = user or self.invitee_user
        if not accepting_user:
            msg = "No user provided to accept invite"
            raise ValueError(msg)

        # Create grants for all resolved workflows
        grants = []
        for workflow in self.get_resolved_workflows():
            grant, _created = WorkflowAccessGrant.objects.get_or_create(
                workflow=workflow,
                user=accepting_user,
                defaults={
                    "granted_by": self.inviter,
                    "is_active": True,
                },
            )

            # If grant already existed but was inactive, reactivate it
            if not _created and not grant.is_active:
                grant.is_active = True
                grant.granted_by = self.inviter
                grant.save(update_fields=["is_active", "granted_by", "modified"])

            grants.append(grant)

        # Update invite status
        self.status = InviteStatus.ACCEPTED
        if not self.invitee_user:
            self.invitee_user = accepting_user
        self.save(update_fields=["status", "invitee_user", "modified"])

        return grants

    def decline(self) -> None:
        """Decline this invite."""
        if self.status != InviteStatus.PENDING:
            return
        self.status = InviteStatus.DECLINED
        self.save(update_fields=["status", "modified"])

    def cancel(self) -> None:
        """Cancel this invite (called by the inviter)."""
        if self.status != InviteStatus.PENDING:
            return
        self.status = InviteStatus.CANCELED
        self.save(update_fields=["status", "modified"])

    @property
    def is_expired(self) -> bool:
        """Check if invite has expired without updating status."""
        from django.utils import timezone

        return self.status == InviteStatus.EXPIRED or timezone.now() >= self.expires_at

    @property
    def is_pending(self) -> bool:
        """Check if invite is still pending and not expired."""
        return self.status == InviteStatus.PENDING and not self.is_expired


class WorkflowSignalMapping(TimeStampedModel):
    """A named signal defined at the workflow level.

    Each row maps a signal name (the author's domain vocabulary) to a
    source path in the submission data.  Resolved once before any step
    runs.  Available in CEL expressions as ``s.<name>`` (or
    ``signal.<name>``).

    The ``on_missing`` field controls what happens when the source path
    cannot be resolved against the submission data:

    - ``error`` (default): the validation run fails immediately with a
      clear message before any step is attempted.
    - ``null``: the signal is injected as ``null`` and the author must
      guard with ``s.name != null`` in CEL expressions.  Accessing a
      null signal without a guard produces a fail-fast evaluation error
      with a message explaining how to fix it.
    """

    workflow = models.ForeignKey(
        "Workflow",
        on_delete=models.CASCADE,
        related_name="signal_mappings",
    )
    name = models.CharField(
        max_length=100,
        help_text="Signal name.  Valid CEL identifier.  Used as s.<name>.",
    )
    source_path = models.CharField(
        max_length=500,
        help_text="Data path resolved against the submission payload.",
    )
    default_value = models.JSONField(
        null=True,
        blank=True,
        help_text="Fallback value when the source path resolves to nothing.",
    )
    on_missing = models.CharField(
        max_length=10,
        choices=[("error", "Fail the run"), ("null", "Inject null")],
        default="error",
    )
    data_type = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Expected type: number, string, boolean.  Empty = infer.",
    )
    position = models.PositiveIntegerField(
        default=0,
        help_text="Display order in the signal mapping editor.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "name"],
                name="unique_signal_name_per_workflow",
            ),
        ]
        ordering = ["position"]

    def clean(self) -> None:
        """Validate signal name is a valid CEL identifier, not reserved,
        and unique across both workflow mappings and promoted outputs.
        """
        from validibot.validations.services.signal_resolution import (
            validate_signal_name,
        )
        from validibot.validations.services.signal_resolution import (
            validate_signal_name_unique,
        )

        errors: dict[str, list[str]] = {}

        # Validate name is valid CEL identifier + not reserved
        name_errors = validate_signal_name(self.name)
        if name_errors:
            errors["name"] = name_errors

        # Cross-table uniqueness check (only if we have a workflow)
        if self.workflow_id:
            unique_errors = validate_signal_name_unique(
                workflow_id=self.workflow_id,
                name=self.name,
                exclude_mapping_id=self.pk,
            )
            if unique_errors:
                errors.setdefault("name", []).extend(unique_errors)

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Run full model validation on every save.

        Django's ``Model.save()`` does NOT call ``clean()`` by default,
        so ORM-level creates (``objects.create()``) would bypass the
        reserved-name and cross-table uniqueness checks.  This override
        ensures those guards always fire.
        """
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"s.{self.name} → {self.source_path}"

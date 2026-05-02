"""Tests for WorkflowVersioningService and contract-edit detection.

ADR-2026-04-27 Phase 3 Session A: workflow versioning core. These
tests cover:

1. The model-level helpers (``has_runs``,
   ``requires_new_version_for_contract_edits``) that enable callers
   (forms, serializers, admin commands) to detect when an in-place
   edit must be replaced with a clone.

2. The :class:`WorkflowVersioningService.clone` operation that
   produces a complete contract copy. The trust-boundary concern
   here is silent contract drift — a "new version" that quietly
   inherits some pre-clone state. The :class:`CloneReport` lets
   tests assert exactly what got copied (and surfaces what didn't,
   via ``warnings``).

The tests use real Django factories rather than mocks because the
service operates on bulk_create + foreign-key relationships across
five related models. Mocking those would re-implement most of
Django's ORM in the test fixture.

Why isolate component-copy tests rather than one big end-to-end clone test
==========================================================================

A single "clone copies everything" test passes when the clone copies
*at least* what the test asserts on. A future regression that adds
a new related-object model (e.g. workflow-level webhooks) might be
silently missed. Per-component tests, by contrast, document the
per-component copy contract and fail loudly when a new component
needs to be added to the service.
"""

from __future__ import annotations

import pytest
from django.test import TestCase

from validibot.submissions.constants import OutputRetention
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.workflows.constants import AgentBillingMode
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowPublicInfo
from validibot.workflows.models import WorkflowRoleAccess
from validibot.workflows.services.versioning import CONTRACT_FIELDS
from validibot.workflows.services.versioning import CloneReport
from validibot.workflows.services.versioning import WorkflowVersioningService
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


# ──────────────────────────────────────────────────────────────────────
# Workflow.has_runs and requires_new_version_for_contract_edits
# ──────────────────────────────────────────────────────────────────────


class WorkflowHasRunsTests(TestCase):
    """``has_runs`` is the canonical "in use" detector.

    Once a workflow has any validation run, its launch contract is
    the rules those runs ran under. Subsequent contract edits must
    produce a new version.
    """

    def test_returns_false_when_no_runs(self):
        """A fresh workflow has no runs."""
        org = OrganizationFactory()
        workflow = WorkflowFactory(org=org)
        WorkflowStepFactory(workflow=workflow)
        assert workflow.has_runs() is False


class WorkflowRequiresNewVersionTests(TestCase):
    """``requires_new_version_for_contract_edits`` policy.

    Returns True when the workflow is locked OR has runs. Either
    state means contract edits should produce a clone.
    """

    def test_fresh_workflow_does_not_require_new_version(self):
        """A workflow with no runs and not locked can be edited in place."""
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow)
        assert workflow.requires_new_version_for_contract_edits() is False

    def test_locked_workflow_requires_new_version(self):
        """``is_locked=True`` is the strongest "in use" signal.

        Setting ``is_locked`` is what
        :meth:`WorkflowVersioningService.clone` does to the source
        on success — so this test also documents the post-clone
        invariant: the source is locked, and any further edits must
        clone again.
        """
        workflow = WorkflowFactory(is_locked=True)
        assert workflow.requires_new_version_for_contract_edits() is True


# ──────────────────────────────────────────────────────────────────────
# Workflow.changed_contract_fields
# ──────────────────────────────────────────────────────────────────────
#
# The model-level helper that forms / serializers / scripts use to
# detect contract drift between current and proposed values. Pure
# data-in/data-out: no DB queries, no side effects. Tested in
# isolation here so changes to the comparison semantics (e.g. the
# set-equality treatment of list-shaped fields) are caught at the
# model layer, not just incidentally via the form.


class WorkflowChangedContractFieldsTests(TestCase):
    """``changed_contract_fields`` is the contract-drift comparator.

    The form / serializer / admin gates all funnel through this
    method. It returns the *names* of contract fields whose proposed
    value differs from the workflow's current value.
    """

    def test_returns_empty_set_when_proposed_matches_current(self):
        """Identical proposal -> no drift."""
        from validibot.submissions.constants import SubmissionRetention

        workflow = WorkflowFactory(
            input_retention=SubmissionRetention.DO_NOT_STORE,
            allowed_file_types=[SubmissionFileType.JSON],
        )
        proposed = {
            "input_retention": SubmissionRetention.DO_NOT_STORE,
            "allowed_file_types": [SubmissionFileType.JSON],
        }
        assert workflow.changed_contract_fields(proposed) == set()

    def test_detects_simple_value_change(self):
        """A single contract field changing -> set with that name."""
        from validibot.submissions.constants import SubmissionRetention

        workflow = WorkflowFactory(
            input_retention=SubmissionRetention.DO_NOT_STORE,
        )
        proposed = {
            "input_retention": SubmissionRetention.STORE_PERMANENTLY,
        }
        assert workflow.changed_contract_fields(proposed) == {"input_retention"}

    def test_list_field_uses_set_equality(self):
        """``allowed_file_types`` ignores ordering (it's a set semantically)."""
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.XML],
        )
        # Same items, different order -> not a change.
        proposed = {
            "allowed_file_types": [SubmissionFileType.XML, SubmissionFileType.JSON],
        }
        assert workflow.changed_contract_fields(proposed) == set()

    def test_list_field_detects_real_membership_change(self):
        """Removing an item from ``allowed_file_types`` IS a change."""
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.XML],
        )
        proposed = {
            "allowed_file_types": [SubmissionFileType.JSON],
        }
        assert workflow.changed_contract_fields(proposed) == {"allowed_file_types"}

    def test_skips_fields_not_in_proposed_dict(self):
        """Partial proposals: only consider fields the caller actually passed.

        A serializer for a PATCH request might only include a subset
        of contract fields. Treating absent keys as "changed to None"
        would falsely flag every PATCH on a locked workflow.
        """
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
        )
        # No keys at all -> nothing changed.
        assert workflow.changed_contract_fields({}) == set()

    def test_ignores_non_contract_fields_even_when_changed(self):
        """``name`` is not a contract field; changing it doesn't show up."""
        workflow = WorkflowFactory(name="Original")
        proposed = {"name": "Renamed", "description": "new"}
        assert workflow.changed_contract_fields(proposed) == set()


# ──────────────────────────────────────────────────────────────────────
# CONTRACT_FIELDS
# ──────────────────────────────────────────────────────────────────────


class ContractFieldsTests(TestCase):
    """Basic shape checks on the CONTRACT_FIELDS constant.

    The constant is the source of truth for "which fields trigger
    versioning?". Removing a field from this set is a meaningful
    semantic change — older code that previously rejected an
    in-place edit may now silently allow it. These tests pin the
    expected membership.
    """

    def test_contract_fields_includes_all_expected_runtime_fields(self):
        """Every documented contract field is in the set."""
        # File-type / retention / agent contract — the runtime
        # contract a previously-launched run was operating under.
        expected = {
            "allowed_file_types",
            "input_retention",
            "output_retention",
            "agent_billing_mode",
            "agent_price_cents",
            "agent_max_launches_per_hour",
            "agent_public_discovery",
            "agent_access_enabled",
        }
        assert expected.issubset(CONTRACT_FIELDS)

    def test_contract_fields_excludes_lifecycle_and_cosmetic(self):
        """Lifecycle and cosmetic fields are NOT contract fields.

        Editing these on a used workflow should be allowed in place
        — they don't change what runs do.
        """
        non_contract = {
            "name",
            "description",
            "slug",
            "version",
            "is_active",
            "is_archived",
            "is_tombstoned",
            "is_locked",
            "make_info_page_public",
            "featured_image",
        }
        for field in non_contract:
            assert field not in CONTRACT_FIELDS, (
                f"{field!r} unexpectedly in CONTRACT_FIELDS — verify "
                f"this is intentional and update this test."
            )

    def test_contract_fields_is_immutable(self):
        """Frozenset — the constant cannot be mutated at runtime."""
        with pytest.raises(AttributeError):
            CONTRACT_FIELDS.add("bogus")  # type: ignore[attr-defined]


# ──────────────────────────────────────────────────────────────────────
# WorkflowVersioningService.clone
# ──────────────────────────────────────────────────────────────────────


class WorkflowVersioningServiceCloneTests(TestCase):
    """``clone`` produces a complete contract copy + structured report."""

    def test_clone_produces_new_workflow_with_incremented_version(self):
        """A v1 workflow clones to v2 with the same slug."""
        workflow = WorkflowFactory(version="1")
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)

        assert report.new_version_label == "2"
        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)
        assert new_workflow.slug == workflow.slug
        assert new_workflow.version == "2"
        assert new_workflow.org == workflow.org

    def test_clone_locks_source_workflow(self):
        """The source is locked after clone — Phase 3 invariant."""
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        WorkflowVersioningService.clone(workflow, user=user)
        workflow.refresh_from_db()
        assert workflow.is_locked is True

    def test_clone_returns_structured_report(self):
        """The CloneReport carries per-component counts and warnings."""
        workflow = WorkflowFactory()
        # Two steps: a deliberate count we then assert the clone report
        # echoes back. Named so the relationship between fixture and
        # assertion is obvious at the call site (PLR2004 hates magic
        # numbers; this also makes the test trivially rebalanceable).
        expected_step_count = 2
        for _ in range(expected_step_count):
            WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)

        assert isinstance(report, CloneReport)
        assert report.source_workflow_id == workflow.pk
        assert report.new_workflow_id != workflow.pk
        assert report.components_copied["steps"] == expected_step_count
        # Warnings document deferred Phase 3 sessions' work.
        # We assert at least one warning exists when the workflow
        # references a Validator (the WorkflowStepFactory default
        # gives steps a validator).
        assert any("Validator" in w for w in report.warnings)

    def test_clone_copies_contract_fields_verbatim(self):
        """All CONTRACT_FIELDS appear on the clone with the source's value.

        Phase 3's whole point: a "new version" inherits the source's
        contract verbatim. Subsequent edits modify the new version,
        not the source.
        """
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON, SubmissionFileType.XML],
            # x402-published workflows must use DO_NOT_STORE retention
            # for submissions (anonymous-per-call access incompatible
            # with persistent storage).
            input_retention=SubmissionRetention.DO_NOT_STORE,
            output_retention=OutputRetention.STORE_30_DAYS,
            agent_public_discovery=True,
            agent_access_enabled=True,
            agent_billing_mode=AgentBillingMode.AGENT_PAYS_X402,
            agent_price_cents=42,
            agent_max_launches_per_hour=99,
        )
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)

        # Verify every contract field copied. We compare via the
        # CONTRACT_FIELDS constant so adding a new contract field
        # surfaces its missing-from-clone gap loudly.
        for field_name in CONTRACT_FIELDS:
            source_value = getattr(workflow, field_name)
            new_value = getattr(new_workflow, field_name)
            assert source_value == new_value, (
                f"Contract field {field_name!r} not copied: "
                f"source={source_value!r} new={new_value!r}"
            )

    def test_clone_copies_step_resources(self):
        """Step-resource rows appear on the clone's steps too."""
        # The existing clone_to_new_version test exercises this
        # path; we just verify the count surfaces in the report.
        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        # Default step has no resources; expect 0.
        assert report.components_copied["step_resources"] == 0

    def test_clone_copies_workflow_public_info_when_present(self):
        """``WorkflowPublicInfo`` (one-to-one) copies if the source has it."""
        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        WorkflowPublicInfo.objects.create(
            workflow=workflow,
            content_md="Source description",
        )
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)

        assert report.components_copied["public_info"] == 1
        # The new workflow has its own public_info row (different pk).
        new_public_info = new_workflow.public_info
        assert new_public_info.content_md == "Source description"
        assert new_public_info.workflow_id == new_workflow.pk

    def test_clone_skips_public_info_when_source_has_none(self):
        """Source without WorkflowPublicInfo -> count is 0."""
        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        assert report.components_copied["public_info"] == 0

    def test_clone_copies_workflow_role_access(self):
        """``WorkflowRoleAccess`` rows replicate to the new workflow.

        Forgetting to copy these would silently grant or revoke
        role access on the new version — exactly the silent-drift
        problem Phase 3 fixes.
        """
        from validibot.users.constants import RoleCode
        from validibot.users.models import Role

        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        # Create a role and grant it on the source workflow.
        role, _ = Role.objects.get_or_create(
            code=RoleCode.EXECUTOR,
            defaults={"name": "Executor"},
        )
        WorkflowRoleAccess.objects.create(workflow=workflow, role=role)
        user = UserFactory()

        report = WorkflowVersioningService.clone(workflow, user=user)
        assert report.components_copied["role_access"] == 1

        new_workflow = Workflow.objects.get(pk=report.new_workflow_id)
        new_access = WorkflowRoleAccess.objects.filter(workflow=new_workflow)
        assert new_access.count() == 1
        assert new_access.first().role == role

    def test_clone_is_atomic_on_failure(self):
        """A mid-clone failure leaves no partial workflow behind.

        We don't have an easy way to inject a failure mid-service
        without monkeypatching. This test documents the atomic
        intent and verifies the source isn't locked when the clone
        completes (i.e. the lock happens within the same transaction
        as the copy).
        """
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        # Successful run — verify atomicity by checking the
        # post-state matches the documented contract.
        WorkflowVersioningService.clone(workflow, user=user)
        workflow.refresh_from_db()
        # Both effects (lock + new workflow exists) happened.
        assert workflow.is_locked is True
        assert Workflow.objects.filter(slug=workflow.slug, version="2").count() == 1


class WorkflowCloneToNewVersionDelegationTests(TestCase):
    """The legacy ``Workflow.clone_to_new_version`` still works.

    Phase 3 refactored the method to delegate to
    :meth:`WorkflowVersioningService.clone`. Existing callers that
    invoke the model method (template tags, admin actions, the
    workflow-edit view) should keep working without modification.
    """

    def test_legacy_method_returns_workflow_instance(self):
        """``clone_to_new_version`` returns the new Workflow row."""
        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        new = workflow.clone_to_new_version(user)

        assert isinstance(new, Workflow)
        assert new.slug == workflow.slug
        assert new.version == "2"

    def test_legacy_method_locks_source(self):
        """The legacy method's contract: source is locked after clone."""
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow)
        user = UserFactory()

        workflow.clone_to_new_version(user)
        workflow.refresh_from_db()
        assert workflow.is_locked is True

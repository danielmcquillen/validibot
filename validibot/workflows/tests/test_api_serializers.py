"""
Tests for the workflow API serializers (WorkflowSlimSerializer, WorkflowFullSerializer).

Verifies:
- Shape of list (slim) and detail (full) responses.
- Nested steps, ruleset, and assertion data in detail responses.
- Query-count bounds to guard against N+1 regressions.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def org(db):
    return OrganizationFactory()


@pytest.fixture
def member(db, org):
    user = UserFactory()
    grant_role(user, org, RoleCode.ADMIN)
    user.set_current_org(org)
    return user


@pytest.fixture
def workflow(db, org, member):
    return WorkflowFactory(
        org=org,
        user=member,
        allowed_file_types=[SubmissionFileType.JSON],
        is_active=True,
    )


@pytest.fixture
def workflow_with_step(db, org, member):
    """Workflow with one validator step that has a step-level ruleset."""
    validator = ValidatorFactory(slug="my-validator", name="My Validator")
    ruleset = RulesetFactory(org=org, ruleset_type=RulesetType.BASIC)
    RulesetAssertionFactory(
        ruleset=ruleset,
        assertion_type=AssertionType.BASIC,
        operator=AssertionOperator.LE,
        target_data_path="score",
        severity=Severity.ERROR,
        rhs={"value": 100},
        message_template="Score must be <= 100.",
    )
    wf = WorkflowFactory(
        org=org,
        user=member,
        allowed_file_types=[SubmissionFileType.JSON],
        is_active=True,
    )
    WorkflowStepFactory(
        workflow=wf,
        order=10,
        name="Schema check",
        description="Validates schema.",
        validator=validator,
        ruleset=ruleset,
        config={"extra_key": "extra_val"},
    )
    return wf


# ---------------------------------------------------------------------------
# List endpoint — WorkflowSlimSerializer shape
# ---------------------------------------------------------------------------


class TestWorkflowListShape:
    """Verify the list endpoint uses WorkflowSlimSerializer."""

    def test_list_returns_200(self, api_client, member, org, workflow):
        """Authenticated member can list workflows."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/")
        assert resp.status_code == HTTPStatus.OK

    def test_list_contains_slim_fields(self, api_client, member, org, workflow):
        """List response includes expected slim fields."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        # Handle both paginated and non-paginated responses
        items = data.get("results", data) if isinstance(data, dict) else data
        assert len(items) >= 1
        item = items[0]
        for field in (
            "id",
            "uuid",
            "slug",
            "name",
            "version",
            "org",
            "is_active",
            "allowed_file_types",
            "url",
        ):
            assert field in item, f"Missing slim field: {field}"

    def test_list_does_not_include_steps(
        self, api_client, member, org, workflow_with_step
    ):
        """List endpoint must NOT include the nested steps array."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        items = data.get("results", data) if isinstance(data, dict) else data
        item = items[0]
        assert "steps" not in item

    def test_list_org_field_is_slug(self, api_client, member, org, workflow):
        """org field on list response is the org slug string, not an integer PK."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/")
        data = resp.json()
        items = data.get("results", data) if isinstance(data, dict) else data
        assert items[0]["org"] == org.slug

    def test_list_url_field_points_to_detail(self, api_client, member, org, workflow):
        """url field on list items points to the detail endpoint."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/")
        data = resp.json()
        items = data.get("results", data) if isinstance(data, dict) else data
        url = items[0]["url"]
        assert f"/orgs/{org.slug}/workflows/" in url
        assert workflow.slug in url


# ---------------------------------------------------------------------------
# Detail endpoint — WorkflowFullSerializer shape
# ---------------------------------------------------------------------------


class TestWorkflowDetailShape:
    """Verify the detail endpoint uses WorkflowFullSerializer."""

    def test_detail_returns_200(self, api_client, member, org, workflow):
        api_client.force_authenticate(user=member)
        resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/{workflow.slug}/")
        assert resp.status_code == HTTPStatus.OK

    def test_detail_contains_full_fields(self, api_client, member, org, workflow):
        """Detail response includes all fields defined on WorkflowFullSerializer."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/{workflow.slug}/")
        data = resp.json()
        for field in (
            "id",
            "uuid",
            "slug",
            "name",
            "version",
            "org",
            "is_active",
            "allowed_file_types",
            "url",
            "is_public",
            "allow_submission_name",
            "allow_submission_meta_data",
            "allow_submission_short_description",
            "input_retention",
            "output_retention",
            "success_message",
            "steps",
        ):
            assert field in data, f"Missing full field: {field}"

    def test_detail_steps_list_present(
        self, api_client, member, org, workflow_with_step
    ):
        """Detail response includes a non-empty steps list."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(
            f"/api/v1/orgs/{org.slug}/workflows/{workflow_with_step.slug}/"
        )
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert isinstance(data["steps"], list)
        assert len(data["steps"]) == 1

    def test_detail_step_fields(self, api_client, member, org, workflow_with_step):
        """Each step in the detail response has the expected fields."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(
            f"/api/v1/orgs/{org.slug}/workflows/{workflow_with_step.slug}/"
        )
        step = resp.json()["steps"][0]
        for field in (
            "id",
            "order",
            "step_number",
            "name",
            "description",
            "validator",
            "action_type",
            "config",
            "ruleset",
        ):
            assert field in step, f"Missing step field: {field}"

    def test_detail_step_validator_fields(
        self, api_client, member, org, workflow_with_step
    ):
        """Nested validator in a step exposes the ValidatorSummarySerializer fields."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(
            f"/api/v1/orgs/{org.slug}/workflows/{workflow_with_step.slug}/"
        )
        validator = resp.json()["steps"][0]["validator"]
        assert validator is not None
        for field in ("slug", "name", "validation_type", "short_description"):
            assert field in validator, f"Missing validator field: {field}"
        assert validator["slug"] == "my-validator"

    def test_detail_step_ruleset_assertions(
        self, api_client, member, org, workflow_with_step
    ):
        """Step-level ruleset includes assertions with the expected fields."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(
            f"/api/v1/orgs/{org.slug}/workflows/{workflow_with_step.slug}/"
        )
        ruleset = resp.json()["steps"][0]["ruleset"]
        assert ruleset is not None
        assert "assertions" in ruleset
        assert len(ruleset["assertions"]) == 1
        assertion = ruleset["assertions"][0]
        for field in (
            "id",
            "order",
            "assertion_type",
            "operator",
            "severity",
            "target_field",
            "rhs",
            "message_template",
        ):
            assert field in assertion, f"Missing assertion field: {field}"

    def test_detail_step_assertion_target_field_from_data_path(
        self, api_client, member, org, workflow_with_step
    ):
        """target_field resolves to target_data_path when no signal
        definition is set."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(
            f"/api/v1/orgs/{org.slug}/workflows/{workflow_with_step.slug}/"
        )
        assertion = resp.json()["steps"][0]["ruleset"]["assertions"][0]
        assert assertion["target_field"] == "score"

    def test_detail_step_number_derived_from_order(
        self, api_client, member, org, workflow_with_step
    ):
        """step_number is 1 when order is 10."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(
            f"/api/v1/orgs/{org.slug}/workflows/{workflow_with_step.slug}/"
        )
        step = resp.json()["steps"][0]
        assert step["order"] == 10  # noqa: PLR2004 — first step order is always 10
        assert step["step_number"] == 1

    def test_detail_action_type_null_for_validator_step(
        self, api_client, member, org, workflow_with_step
    ):
        """action_type is null for validator steps."""
        api_client.force_authenticate(user=member)
        resp = api_client.get(
            f"/api/v1/orgs/{org.slug}/workflows/{workflow_with_step.slug}/"
        )
        step = resp.json()["steps"][0]
        assert step["action_type"] is None


# ---------------------------------------------------------------------------
# N+1 query guard
# ---------------------------------------------------------------------------


class TestWorkflowDetailQueryCount:
    """Guard against N+1 query regressions on the detail endpoint."""

    def test_detail_query_count_bounded(self, api_client, member, org):
        """
        Detail endpoint query count stays bounded regardless of step/assertion count.

        We create 3 steps each with a ruleset containing 2 assertions, then confirm
        the total query count stays within a reasonable fixed ceiling. The prefetch
        in get_object() should keep it well under (3 steps x 2 assertions) queries.
        """
        validator = ValidatorFactory()
        wf = WorkflowFactory(
            org=org, user=member, allowed_file_types=[SubmissionFileType.JSON]
        )
        for i in range(3):
            ruleset = RulesetFactory(org=org, ruleset_type=RulesetType.BASIC)
            RulesetAssertionFactory(ruleset=ruleset, target_data_path=f"field_{i}_a")
            RulesetAssertionFactory(ruleset=ruleset, target_data_path=f"field_{i}_b")
            WorkflowStepFactory(
                workflow=wf,
                order=(i + 1) * 10,
                validator=validator,
                ruleset=ruleset,
            )

        api_client.force_authenticate(user=member)

        with CaptureQueriesContext(connection) as ctx:
            resp = api_client.get(f"/api/v1/orgs/{org.slug}/workflows/{wf.slug}/")

        assert resp.status_code == HTTPStatus.OK
        # A well-prefetched response should need far fewer than 20 queries even with
        # 3 steps x 2 assertions. This ceiling guards against obvious N+1 regressions
        # while staying loose enough not to fail on minor refactors.
        assert len(ctx.captured_queries) < 20, (  # noqa: PLR2004 — 20 is an intentional ceiling
            f"Detail endpoint issued {len(ctx.captured_queries)} queries — "
            "possible N+1 regression. Check prefetch_related in get_object()."
        )

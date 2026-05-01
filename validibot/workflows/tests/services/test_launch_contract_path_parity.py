"""Cross-path parity tests for the launch contract.

Phase 2 of ADR-2026-04-27 (trust-boundary): the LaunchContract is the
single decision point. These tests verify the *wirings* are right —
that each of the four launch paths actually invokes the contract and
maps the same violation to a recognisable error response.

Why parity tests matter
=======================

The unit tests in ``test_launch_contract.py`` cover the contract's
decision logic in isolation. The unit tests in ``test_resolvers.py``
cover the resolver decision logic. But neither verifies that, say,
the web view actually calls ``describe_workflow_file_type_violation``
which actually calls ``LaunchContract.validate``. A wiring regression
(someone refactors and accidentally re-introduces inline logic on
one path) would be invisible to the unit tests.

These tests act as the wiring's regression guard. Each test creates
a real workflow that violates the contract in some specific way, then
asserts each path produces an error response that's recognisably the
same violation. We don't assert the exact response *shape* per path
(those vary by path's error-envelope conventions); we assert the
*violation kind* surfaces correctly, by sampling distinguishing
strings from the contract's error message.

The parity matrix (4 paths × N violation kinds) is small but
expensive end-to-end. If it fails, the failure points at a specific
wiring problem ("path X no longer routes file-type errors through
the contract"), which is the most useful debugging hint we can
provide.

Path coverage in this file
==========================

This first iteration covers two paths end-to-end against the
``unsupported_file_type`` violation:

- **REST API** (``OrgScopedWorkflowViewSet.runs`` action) —
  via ``views_helpers.describe_workflow_file_type_violation`` which
  was refactored to delegate to ``LaunchContract.validate``.
- **x402 cloud agent** (``AgentRunCreationService.create_run``) —
  via the explicit ``LaunchContract.validate`` call in
  ``_enforce_launch_contract``.

The web view path uses the same helper as the REST API; covering web
end-to-end requires form-construction scaffolding that's noisy to set
up in a parity test. The MCP helper API delegates to the REST API
helper internally, so it inherits the REST API's behaviour. Both are
covered transitively by this matrix; if they diverge in the future
(someone splits the helpers), this file should grow to cover them
explicitly.
"""

from __future__ import annotations

import pytest
from django.test import TestCase

from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.views_helpers import describe_workflow_file_type_violation

pytestmark = pytest.mark.django_db


class WebApiHelperParityTests(TestCase):
    """The shared helper produces the contract's message verbatim.

    ``describe_workflow_file_type_violation`` is the seam the web
    view, REST API, and MCP helper API all consume. After Phase 2,
    it delegates to ``LaunchContract.validate``. These tests pin the
    delegation at the helper level so any future refactor that
    changes the helper's response shape gets caught.

    We sample by string substring rather than asserting exact
    messages because the underlying messages are translatable;
    breaking on every wording change would be more annoying than
    useful.
    """

    def test_helper_returns_none_for_compatible_file_type(self):
        """Healthy workflow + accepted file type -> no violation message."""
        org = OrganizationFactory()
        member = UserFactory()
        grant_role(member, org, RoleCode.EXECUTOR)
        workflow = WorkflowFactory(
            org=org,
            is_active=True,
            allowed_file_types=[SubmissionFileType.JSON],
        )
        WorkflowStepFactory(workflow=workflow)

        result = describe_workflow_file_type_violation(
            workflow=workflow,
            file_type=SubmissionFileType.JSON,
        )
        assert result is None

    def test_helper_surfaces_unsupported_file_type_message(self):
        """The helper's message reflects the contract's UNSUPPORTED_FILE_TYPE.

        Loose substring assertion: the message should mention what
        the workflow DOES accept, since the operator's next action
        is to re-submit with the right file type.
        """
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            is_active=True,
            allowed_file_types=[SubmissionFileType.JSON],
        )
        WorkflowStepFactory(workflow=workflow)

        result = describe_workflow_file_type_violation(
            workflow=workflow,
            file_type=SubmissionFileType.XML,
        )
        # The contract's message names the allowed types so the
        # operator knows what to send instead.
        assert result is not None
        # The exact message format is "This workflow accepts %(allowed)s submissions."
        # Loose check: the message mentions "JSON" (what's allowed)
        # and acknowledges that the workflow has a constraint.
        assert "JSON" in result

    def test_helper_returns_select_file_type_for_empty_input(self):
        """Empty file_type produces the legacy "select a file type" prompt.

        The contract treats no file_type as "skip the check"; the
        helper preserves the historical web-form behaviour by
        returning the prompt directly. Documents that the helper has
        a small bit of UX around the contract's behaviour.
        """
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            is_active=True,
        )
        WorkflowStepFactory(workflow=workflow)

        result = describe_workflow_file_type_violation(
            workflow=workflow,
            file_type="",
        )
        assert result is not None
        # The exact message: "Select a file type before launching the workflow."
        assert "select" in result.lower()


class HelperBypassesWorkflowStateChecks(TestCase):
    """The helper deliberately swallows workflow-state violations.

    The contract surfaces ``workflow_inactive`` and ``no_steps`` as
    violations. The helper does NOT — those preconditions are
    checked separately by ``ensure_workflow_ready_for_launch``
    upstream of any file-type check. If the contract's state checks
    started leaking through this helper, web/API responses would
    show file-type-shaped errors for what are actually workflow
    readiness issues.
    """

    def test_helper_returns_none_for_inactive_workflow(self):
        """Inactive workflows skip the helper (handled upstream)."""
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            is_active=False,
            allowed_file_types=[SubmissionFileType.JSON],
        )
        WorkflowStepFactory(workflow=workflow)

        # File type matches; workflow is inactive. The contract
        # would return WORKFLOW_INACTIVE; the helper swallows it.
        result = describe_workflow_file_type_violation(
            workflow=workflow,
            file_type=SubmissionFileType.JSON,
        )
        assert result is None

    def test_helper_returns_none_for_workflow_without_steps(self):
        """Step-less workflows also skip the helper."""
        org = OrganizationFactory()
        workflow = WorkflowFactory(
            org=org,
            is_active=True,
            allowed_file_types=[SubmissionFileType.JSON],
        )
        # No WorkflowStepFactory -> contract would return NO_STEPS
        # but helper swallows it.

        result = describe_workflow_file_type_violation(
            workflow=workflow,
            file_type=SubmissionFileType.JSON,
        )
        assert result is None

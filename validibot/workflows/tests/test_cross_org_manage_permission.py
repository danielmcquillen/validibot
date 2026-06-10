"""Regression tests for cross-org workflow management authorization.

These tests pin down the fix for the cross-org / public-workflow
write-escalation reported in the 2026-06-10 security review (finding H1).

The defect: ``WorkflowObjectMixin`` resolves a workflow *cross-org* —
``get_workflow_queryset_for_access()`` deliberately includes public
workflows and guest-granted foreign workflows so those users can reach the
detail page. The mutation gate ``WorkflowAccessMixin.user_can_manage_workflow``
previously checked ``WORKFLOW_EDIT`` against the *caller's current org*
(``membership_for_current_org()``) rather than the *resolved workflow's*
org. Because every user is OWNER of their auto-provisioned personal
workspace, that current-org check effectively always passed — so any
authenticated user could edit (add/delete steps, assertions, signal
mappings on, or deactivate) another org's public or guest-shared workflow,
silently corrupting the validation logic behind signed attestations.

The fix makes the gate object-scoped: it delegates to
``Workflow.can_edit(user=...)``, which routes through ``OrgPermissionBackend``
and derives the org from the workflow object itself. These tests assert the
gate is now keyed on the workflow's org, not the caller's current org.
"""

from __future__ import annotations

import pytest

from validibot.users.constants import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.tests.factories import WorkflowFactory


class _StubManageView(WorkflowObjectMixin):
    """Minimal mixin host that returns a fixed workflow and request.

    We exercise ``user_can_manage_workflow`` directly rather than driving a
    full HTTP request so the test pins the *authorization decision* (the
    thing that was wrong) without coupling to URL routing or the org-switch
    middleware. ``get_workflow`` is overridden to hand back the in-context
    workflow, mirroring what ``WorkflowObjectMixin`` resolves from the URL.
    """

    def __init__(self, *, request, workflow):
        self.request = request
        self._stub_workflow = workflow

    def get_workflow(self):  # type: ignore[override]
        return self._stub_workflow


class _Request:
    """Tiny stand-in for an HttpRequest carrying just an authenticated user."""

    def __init__(self, user):
        self.user = user


@pytest.fixture
def attacker_and_victim_workflow(db):
    """An attacker (OWNER of their own org) and a PUBLIC workflow in another org.

    This is the exact precondition for the escalation: the attacker holds
    full rights in org A, and the victim workflow lives in org B and is
    reachable cross-org because it is public.
    """
    org_a = OrganizationFactory()
    org_b = OrganizationFactory()

    attacker = UserFactory()
    grant_role(attacker, org_a, RoleCode.OWNER)
    attacker.set_current_org(org_a)

    victim_workflow = WorkflowFactory(org=org_b, is_public=True)
    return attacker, victim_workflow, org_b


def test_owner_of_other_org_cannot_manage_foreign_public_workflow(
    attacker_and_victim_workflow,
):
    """A user who is OWNER of org A must NOT be able to manage org B's workflow.

    This is the core of H1: management must be scoped to the *workflow's*
    org, not the caller's current org. Before the fix this returned True
    (the attacker's own-org OWNER role satisfied the current-org check),
    which let them mutate another org's public workflow.
    """
    attacker, victim_workflow, _org_b = attacker_and_victim_workflow
    view = _StubManageView(request=_Request(attacker), workflow=victim_workflow)

    assert view.user_can_manage_workflow() is False


def test_member_with_edit_in_workflow_org_can_manage(attacker_and_victim_workflow):
    """A user with WORKFLOW_EDIT in the workflow's own org can still manage it.

    The fix must not regress the legitimate path: an author/owner in the
    workflow's org keeps management rights. Without this assertion a fix
    that simply denied everyone would also pass the negative test above.
    """
    _attacker, victim_workflow, org_b = attacker_and_victim_workflow

    legit = UserFactory()
    grant_role(legit, org_b, RoleCode.OWNER)
    legit.set_current_org(org_b)

    view = _StubManageView(request=_Request(legit), workflow=victim_workflow)

    assert view.user_can_manage_workflow() is True


def test_can_edit_is_org_scoped_on_the_model(attacker_and_victim_workflow):
    """``Workflow.can_edit`` itself is org-scoped (the mechanism the fix relies on).

    ``user_can_manage_workflow`` delegates here, so we assert the model-level
    check rejects a cross-org OWNER directly — documenting *why* the
    delegation is safe.
    """
    attacker, victim_workflow, _org_b = attacker_and_victim_workflow

    assert victim_workflow.can_edit(user=attacker) is False

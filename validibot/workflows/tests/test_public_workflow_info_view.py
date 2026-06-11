"""Public workflow info-page visibility tests.

The info page (``workflows/<uuid>/info/`` -> :class:`PublicWorkflowInfoView`)
is the per-workflow counterpart to the public directory listing. Like the
listing it is a mixed anonymous/member surface, so its visibility contract
must match what both the directory and the "Workflow Public Info Page" status
card advertise:

* **Anonymous visitors** may open a workflow's info page only once its author
  has published it (``make_info_page_public=True``).
* **A signed-in teammate with access** may open the info page even while it is
  still private -- the status card literally promises "Only teammates with
  access can currently view the workflow info page," and the public directory
  already lists the workflow for that teammate.

These tests pin both halves of that contract. The fourth test guards the
security boundary that matters most after widening visibility: the
authenticated branch grants access to "teammates with access," NOT to "anyone
who happens to be logged in." A regression there would silently turn every
private info page into a disclosure to any authenticated user.

Regression context: the info view originally filtered only on
``make_info_page_public=True``, so a teammate with view access was 404'd on the
info page despite being shown the workflow everywhere else. The view now mirrors
:class:`PublicWorkflowListView` by OR-ing in ``Workflow.objects.for_user(user)``.
"""

from __future__ import annotations

import json
from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def _info_url(workflow) -> str:
    """Resolve a workflow's public info URL from its UUID.

    The route is keyed by ``uuid`` (not the integer PK) precisely because
    it is a public, link-shareable surface -- an unguessable identifier
    keeps the workflow space from being enumerable.
    """
    return reverse(
        "workflow_public_info",
        kwargs={"workflow_uuid": workflow.uuid},
    )


def test_published_info_page_visible_to_anonymous(client):
    """A published info page must load for anonymous visitors.

    This is the baseline the "Visible" toggle exists to provide: flipping
    ``make_info_page_public`` on is what lets an unauthenticated person open
    the link at all. If this breaks, public sharing is broken.
    """
    workflow = WorkflowFactory(make_info_page_public=True)

    response = client.get(_info_url(workflow))

    assert response.status_code == HTTPStatus.OK


def test_private_info_page_hidden_from_anonymous(client):
    """A private info page must 404 for anonymous visitors.

    The access gate is the entire point of the "Private" state: an
    unpublished workflow should be indistinguishable from a non-existent one
    to the public -- a 404, never a 403 that would confirm the UUID maps to a
    real workflow. This also proves the new authenticated-teammate branch did
    not accidentally open the page to everyone.
    """
    workflow = WorkflowFactory(make_info_page_public=False)

    response = client.get(_info_url(workflow))

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_private_info_page_visible_to_member_with_access(client):
    """A teammate with view access must see a *private* info page.

    This is the regression the view's OR-branch fixes. A non-creator member
    holding the read-only ``WORKFLOW_VIEWER`` role -- the exact "teammate
    with access" the status card describes -- opens a page that is still
    marked private and must get 200, not the historical 404. We use a
    distinct user (not ``workflow.user``) so the access is proven through the
    org-membership path in ``for_user``, not the creator shortcut.
    """
    workflow = WorkflowFactory(make_info_page_public=False)
    teammate = UserFactory()
    membership = Membership.objects.create(
        user=teammate,
        org=workflow.org,
        is_active=True,
    )
    # WORKFLOW_VIEWER grants WORKFLOW_VIEW but not WORKFLOW_LAUNCH, so this
    # also exercises the "view-only teammate" case: they can see the page
    # without the launch affordance, which is gated separately by
    # ``can_execute`` in get_context_data.
    membership.add_role(RoleCode.WORKFLOW_VIEWER)
    client.force_login(teammate)

    response = client.get(_info_url(workflow))

    assert response.status_code == HTTPStatus.OK


def test_private_info_page_hidden_from_unrelated_user(client):
    """A signed-in user from another org must NOT see a private page.

    This is the security boundary introduced by widening visibility. The
    authenticated branch must mean "teammates with access," never "anyone who
    is logged in." An arbitrary user with no membership, grant, or guest
    access to the workflow's org has to receive the same 404 an anonymous
    visitor would. Losing this assertion would convert every private info
    page into a logged-in-user disclosure -- the worst-case regression of
    this change.
    """
    workflow = WorkflowFactory(make_info_page_public=False)
    outsider = UserFactory()  # deliberately no Membership in workflow.org
    client.force_login(outsider)

    response = client.get(_info_url(workflow))

    assert response.status_code == HTTPStatus.NOT_FOUND


# Tabular schema visibility
# The author-controlled display_schema flag is a disclosure boundary: the
# public page may describe the step, but must reveal its table contract only
# when the author explicitly enables schema display.


@pytest.mark.parametrize(
    ("display_schema", "details_visible"),
    [(False, False), (True, True)],
)
def test_tabular_details_follow_schema_visibility_setting(
    client,
    display_schema,
    details_visible,
):
    """The public table contract must exactly follow ``display_schema``.

    Tabular details expose column names, types, constraints, and CSV dialect.
    Keeping the step card visible while gating that contract proves an unchecked
    "User can view schema" field does not disclose authoring information.
    """
    workflow = WorkflowFactory(make_info_page_public=True)
    validator = ValidatorFactory(validation_type=ValidationType.TABULAR)
    ruleset = RulesetFactory(
        org=workflow.org,
        ruleset_type=RulesetType.TABULAR,
        rules_text=json.dumps(
            {
                "fields": [
                    {
                        "name": "private_meter_id",
                        "type": "string",
                        "constraints": {"required": True},
                    },
                ],
            },
        ),
    )
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        display_schema=display_schema,
    )

    response = client.get(_info_url(workflow))

    assert response.status_code == HTTPStatus.OK
    public_step = next(item for item in response.context["steps"] if item.pk == step.pk)
    assert (public_step.public_tabular is not None) is details_visible
    html = response.content.decode()
    assert (f'id="tabular-details-{step.pk}"' in html) is details_visible
    assert ("private_meter_id" in html) is details_visible

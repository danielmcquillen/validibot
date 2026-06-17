"""Tests for ``WorkflowJsonView`` object resolution.

``WorkflowJsonView`` (``validibot.workflows.views.management``) renders a
read-only JSON dump of a workflow's full structure. This suite covers the
defensive object lookup: a request for a workflow pk that does not exist must
produce a clean 404, not an unhandled ``Workflow.DoesNotExist`` that surfaces
as a 500.
"""

from __future__ import annotations

import pytest
from django.http import Http404

from validibot.workflows.views.management import WorkflowJsonView

pytestmark = pytest.mark.django_db


class TestWorkflowJsonViewGetObject:
    """``get_object()`` must 404 on a missing pk, not 500.

    Regression for ADR 04-23 §bug.workflow_json_doesnotexist.
    The override previously ended in a bare ``.get()``, so a non-existent pk
    raised ``Workflow.DoesNotExist`` and bubbled up as a 500. Switching to
    ``get_object_or_404`` converts the miss into an ``Http404`` (→ 404). Access
    control is unaffected: it is enforced upstream in
    ``WorkflowObjectMixin.get_workflow()`` before this lookup runs.
    """

    def test_missing_pk_raises_http404_not_doesnotexist(self):
        """A pk with no matching ``Workflow`` raises ``Http404``.

        This is the regression guard: against the old bare ``.get()`` this
        call raised ``Workflow.DoesNotExist`` (an unhandled 500); it must now
        raise ``Http404`` so Django renders a 404.
        """
        view = WorkflowJsonView()
        view.kwargs = {"pk": 99999999}

        with pytest.raises(Http404):
            view.get_object()

from __future__ import annotations

import pytest
from django.core.management import call_command

from simplevalidations.validations.tests.factories import ValidatorFactory
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.models import WorkflowPublicInfo

pytestmark = pytest.mark.django_db


def test_create_dummy_workflows_creates_records():
    ValidatorFactory()
    initial_count = Workflow.objects.count()

    call_command("create_dummy_workflows", count=3)

    workflows = Workflow.objects.order_by("-created")[:3]
    assert Workflow.objects.count() == initial_count + 3

    for workflow in workflows:
        assert workflow.steps.count() >= 2  # noqa: PLR2004
        public_info = WorkflowPublicInfo.objects.get(workflow=workflow)
        assert public_info.content_html
        assert workflow.project is not None
        assert workflow.make_info_public is True

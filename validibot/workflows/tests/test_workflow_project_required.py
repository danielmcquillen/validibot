"""
Tests for the rule that every workflow must belong to a project.

Runs started from a workflow default to that workflow's project, and several
downstream surfaces (analytics, quotas, project-scoped views) assume a non-null
project. The ``project`` column stays nullable at the database level for
historical rows and for ``on_delete=SET_NULL`` when a project is deleted, but
creating or saving a workflow with no project is not allowed.

This rule is enforced at the model layer (``Workflow.clean()``), which means it
covers every write path that goes through ``full_clean()`` — including
``Workflow.save()``, the web form, the .vaf importer, and version cloning. The
form-level and importer-level coverage live alongside their own suites; here we
pin the model-level guarantee directly.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def test_clean_rejects_workflow_without_a_project():
    """``Workflow.clean()`` raises a project-scoped error when project is unset.

    This is the backstop that protects non-form write paths: any code that
    builds a Workflow without a project and calls ``full_clean()`` (as
    ``save()`` does) gets a clear validation error rather than silently
    persisting a project-less row.
    """
    workflow = WorkflowFactory.build(project=None)

    with pytest.raises(ValidationError) as excinfo:
        workflow.clean()

    assert "project" in excinfo.value.message_dict


def test_save_rejects_workflow_without_a_project():
    """Saving a project-less workflow fails because ``save()`` runs full_clean().

    Verifies the guard holds end-to-end through the normal persistence path,
    not just when ``clean()`` is called directly.
    """
    workflow = WorkflowFactory.build(project=None)

    with pytest.raises(ValidationError):
        workflow.save()

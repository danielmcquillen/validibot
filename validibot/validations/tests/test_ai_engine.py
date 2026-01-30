"""
Tests for the AI-assist validation engine.

The AI engine is not yet implemented and raises NotImplementedError.
These tests verify that behavior and serve as a placeholder for future tests
when the AI integration is built.
"""

from __future__ import annotations

import json

import pytest

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import ValidationType
from validibot.validations.engines.ai import AiAssistEngine
from validibot.validations.models import Validator
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _ai_validator() -> Validator:
    validator, _ = Validator.objects.get_or_create(
        validation_type=ValidationType.AI_ASSIST,
        slug="ai-assist",
        defaults={"name": "AI Assist", "description": "AI assisted"},
    )
    return validator


def test_ai_engine_raises_not_implemented():
    """AI engine should raise NotImplementedError until AI integration is built."""
    workflow = WorkflowFactory()
    validator = _ai_validator()
    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        project=None,
        content=json.dumps({"test": "data"}),
        file_type=SubmissionFileType.JSON,
    )
    engine = AiAssistEngine(config={})

    with pytest.raises(NotImplementedError) as exc_info:
        engine.validate(validator=validator, submission=submission, ruleset=None)

    assert "AI-assisted validation is not yet implemented" in str(exc_info.value)
    assert "AI model API" in str(exc_info.value)

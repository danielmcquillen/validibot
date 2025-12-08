from __future__ import annotations

import json

import pytest

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import Severity
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


def test_ai_engine_policy_violation():
    workflow = WorkflowFactory()
    validator = _ai_validator()
    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        project=None,
        content=json.dumps({"zones": [{"cooling_setpoint": 14}]}),
        file_type=SubmissionFileType.JSON,
    )
    config = {
        "template": "policy_check",
        "selectors": ["$.zones[*].cooling_setpoint"],
        "policy_rules": [
            {
                "id": "cooling-min",
                "path": "$.zones[*].cooling_setpoint",
                "operator": ">=",
                "value": 18,
                "message": "Cooling setpoint must be ≥18°C",
            },
        ],
        "mode": "BLOCKING",
        "cost_cap_cents": 12,
    }
    engine = AiAssistEngine(config=config)
    result = engine.validate(validator=validator, submission=submission, ruleset=None)
    assert result.passed is False
    error_messages = [issue.message for issue in result.issues]
    assert any("Cooling setpoint" in message for message in error_messages)
    assert any(issue.severity == Severity.ERROR for issue in result.issues)


def test_ai_engine_json_only_warning():
    workflow = WorkflowFactory()
    validator = _ai_validator()
    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        project=None,
        content="<root><value>42</value></root>",
        file_type=SubmissionFileType.XML,
    )
    engine = AiAssistEngine(config={})
    result = engine.validate(validator=validator, submission=submission, ruleset=None)
    assert result.passed is False
    assert result.issues[0].severity == Severity.WARNING
    assert "supports JSON" in result.issues[0].message


def test_ai_engine_generates_heuristic_warnings():
    workflow = WorkflowFactory()
    validator = _ai_validator()
    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        project=None,
        content=json.dumps(
            {
                "Schedule": [1 for _ in range(24)],
                "zones": [{"setpoint": 50}, {"load": 2000000}],
            },
        ),
        file_type=SubmissionFileType.JSON,
    )
    engine = AiAssistEngine(config={"template": "ai_critic", "selectors": []})
    result = engine.validate(validator=validator, submission=submission, ruleset=None)
    messages = " ".join(str(issue.message) for issue in result.issues)
    assert "24/7" in messages or "Temperature" in messages
    assert any(
        issue.severity in {Severity.WARNING, Severity.INFO} for issue in result.issues
    )

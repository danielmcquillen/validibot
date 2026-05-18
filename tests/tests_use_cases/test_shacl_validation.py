"""End-to-end SHACL validation tests.

Mirrors the existing JSON Schema + XSD use-case test patterns:

1. Build a minimal workflow with a SHACL validator + ruleset.
2. POST a Turtle submission via the API.
3. Poll the run to completion.
4. Assert the run status and the issues list.

The tests exercise the full Django path — API → validation run launch
→ SHACLValidator → SHACL report → findings — without any external
infrastructure (no Docker, no Cloud Run, no network). SHACL is a
built-in validator, so the entire flow runs in-process during the test.

These tests live alongside the other use-case tests for parity. The
finer-grained engine tests for SHACL (parse, severity mapping, signal
extraction, library-validator merge) live next to the SHACL package at
``validibot/validations/tests/test_validators/test_shacl_*.py``.
"""

from __future__ import annotations

import logging

import pytest
from django.urls import reverse
from rest_framework.status import HTTP_200_OK

from tests.helpers.polling import extract_issues
from tests.helpers.polling import normalize_poll_url
from tests.helpers.polling import poll_until_complete
from tests.helpers.polling import start_workflow_url
from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import RoleCode
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.django_db


@pytest.fixture
def workflow_context(load_shacl_asset, api_client):
    """Build a minimal SHACL workflow + authenticated API client.

    The workflow has a single step using the system SHACL validator
    with the person-shapes ruleset attached. Submissions accepted as
    plain text (Turtle).
    """
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    user.set_current_org(org)
    grant_role(user, org, RoleCode.EXECUTOR)

    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
    )

    shapes = load_shacl_asset("example_person_shapes.ttl")
    ontology = load_shacl_asset("example_person_ontology.ttl")

    ruleset = RulesetFactory(
        org=org,
        user=user,
        ruleset_type=RulesetType.SHACL,
        rules_text=shapes,
        metadata={
            "ontology_text": ontology,
            "inference_mode": "rdfs",
            "advanced_shacl": True,
            "submission_format": "auto",
            "bundled_standards": [],
        },
    )

    workflow = WorkflowFactory(
        org=org,
        user=user,
        allowed_file_types=[SubmissionFileType.TEXT],
    )
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        ruleset=ruleset,
        order=1,
    )

    api_client.force_authenticate(user=user)

    return {
        "org": org,
        "user": user,
        "validator": validator,
        "ruleset": ruleset,
        "workflow": workflow,
        "step": step,
        "client": api_client,
    }


def _run_and_poll(
    client,
    workflow,
    content: str,
    content_type: str = "text/turtle",
) -> dict:
    """POST a submission, follow the Location header, poll until done.

    Lifted from ``test_xsd_validation.py``'s ``_run_and_poll`` helper
    with the content_type defaulted to ``text/turtle`` — Turtle is the
    most common RDF serialization and matches the ``.ttl`` examples in
    ``tests/assets/shacl/``.
    """
    start_url = start_workflow_url(workflow)
    resp = client.post(start_url, data=content, content_type=content_type)
    assert resp.status_code in (200, 201, 202), resp.content

    loc = resp.headers.get("Location") or resp.headers.get("location") or ""
    poll_url = normalize_poll_url(loc)
    if not poll_url:
        data = {}
        try:
            data = resp.json()
        except Exception as exc:
            logger.debug("Could not parse JSON response: %s", exc)
        run_id = data.get("id")
        if run_id:
            org_slug = workflow.org.slug
            try:
                poll_url = reverse(
                    "api:org-runs-detail",
                    kwargs={"org_slug": org_slug, "pk": run_id},
                )
            except Exception as exc:
                logger.debug("Could not reverse org-runs-detail: %s", exc)
                poll_url = f"/api/v1/orgs/{org_slug}/runs/{run_id}/"

    data, last_status = poll_until_complete(client, poll_url)
    assert last_status == HTTP_200_OK, f"Polling failed: {last_status} {data}"
    return data


@pytest.mark.django_db
class TestShaclValidation:
    """End-to-end SHACL validation against ``example_person_shapes.ttl``.

    Covers the two paths a workflow author cares about: a passing
    submission (returns SUCCEEDED, no issues) and a failing submission
    (returns FAILED with a SHACL constraint violation surfaced as an
    issue).

    These tests intentionally use the system SHACL validator with a
    minimal Person shape rather than ASHRAE 223P shapes — keeping the
    fixtures small (under 30 lines of Turtle each) means the tests are
    fast, the fixtures are diff-friendly, and the test doesn't depend
    on ASHRAE-copyrighted content.
    """

    def test_shacl_valid_submission_succeeds(
        self,
        load_shacl_asset,
        workflow_context,
    ):
        """A submission satisfying the shape succeeds with no issues."""
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        submission = load_shacl_asset("valid_person.ttl")

        data = _run_and_poll(client, workflow, submission)

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.SUCCEEDED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) == 0, f"Expected no issues, got: {issues}"

    def test_shacl_invalid_submission_fails_with_violation(
        self,
        load_shacl_asset,
        workflow_context,
    ):
        """A submission violating the shape fails with a constraint message."""
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]
        submission = load_shacl_asset("invalid_person.ttl")

        data = _run_and_poll(client, workflow, submission)

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) >= 1, "Expected at least one issue for invalid submission"

        joined = " | ".join(str(issue) for issue in issues)
        # The shape's sh:message includes "name" — verify it propagated
        # through to the visible issue so authors can act on the failure.
        assert "name" in joined.lower(), (
            f"Expected the shape's 'name' message in issues, got: {issues}"
        )

    def test_shacl_unparseable_submission_fails_with_parse_error(
        self,
        workflow_context,
    ):
        """A submission that isn't valid Turtle/RDF fails with a parse error.

        Operators occasionally upload the wrong file type. The
        validator should fail cleanly with a parse-error finding
        rather than crashing the run.
        """
        client = workflow_context["client"]
        workflow = workflow_context["workflow"]

        data = _run_and_poll(client, workflow, "this is not turtle <<<<")

        run_status = (data.get("status") or data.get("state") or "").upper()
        assert run_status == ValidationRunStatus.FAILED, (
            f"Unexpected status: {run_status} payload={data}"
        )
        issues = extract_issues(data)
        assert isinstance(issues, list)
        assert len(issues) >= 1, "Expected at least one issue for bad RDF"

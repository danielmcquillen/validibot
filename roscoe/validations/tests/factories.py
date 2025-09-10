from importlib import metadata

import factory
from factory.django import DjangoModelFactory

from roscoe.projects.tests.factories import ProjectFactory
from roscoe.submissions.tests.factories import SubmissionFactory
from roscoe.users.tests.factories import OrganizationFactory
from roscoe.validations.constants import RulesetType
from roscoe.validations.constants import Severity
from roscoe.validations.constants import StepStatus
from roscoe.validations.constants import ValidationRunStatus
from roscoe.validations.constants import ValidationType
from roscoe.validations.models import Artifact
from roscoe.validations.models import Ruleset
from roscoe.validations.models import ValidationFinding
from roscoe.validations.models import ValidationRun
from roscoe.validations.models import ValidationStepRun
from roscoe.validations.models import Validator
from roscoe.workflows.tests.factories import WorkflowFactory
from roscoe.workflows.tests.factories import WorkflowStepFactory


class RulesetFactory(DjangoModelFactory):
    class Meta:
        model = Ruleset

    org = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Test Ruleset {n}")
    ruleset_type = RulesetType.JSON_SCHEMA
    version = "1"
    # Give an empty schema by default to avoid validation errors
    metadata = factory.Dict(
        {
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {},
            },
        },
    )


class ValidatorFactory(DjangoModelFactory):
    class Meta:
        model = Validator

    slug = factory.Sequence(lambda n: f"test-validator-{n}")
    description = factory.Faker("text", max_nb_chars=200)
    name = factory.Sequence(lambda n: f"Test Validator {n}")
    validation_type = ValidationType.JSON_SCHEMA
    version = "1"
    default_ruleset = factory.SubFactory(RulesetFactory)


class ValidationRunFactory(DjangoModelFactory):
    class Meta:
        model = ValidationRun

    submission = factory.SubFactory(SubmissionFactory)
    workflow = factory.SubFactory(WorkflowFactory)
    org = factory.SubFactory(OrganizationFactory)
    project = factory.SubFactory(ProjectFactory)
    status = ValidationRunStatus.PENDING


class ValidationStepRunFactory(DjangoModelFactory):
    class Meta:
        model = ValidationStepRun

    run = factory.SubFactory(ValidationRunFactory)
    step = factory.SubFactory(WorkflowStepFactory)
    step_order = factory.Sequence(lambda n: n * 10)
    status = StepStatus.PENDING
    summary = factory.Faker("sentence")


class ValidationFindingFactory(DjangoModelFactory):
    class Meta:
        model = ValidationFinding

    step_run = factory.SubFactory(ValidationStepRunFactory)
    severity = Severity.ERROR
    message = factory.Faker("sentence")
    line_number = factory.Faker("random_int", min=1, max=1000)
    column_number = factory.Faker("random_int", min=1, max=100)


class ArtifactFactory(DjangoModelFactory):
    class Meta:
        model = Artifact

    step_run = factory.SubFactory(ValidationStepRunFactory)
    org = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"artifact-{n}.txt")
    description = factory.Faker("sentence")
    # Note: file field is optional for testing

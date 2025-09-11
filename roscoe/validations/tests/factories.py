from importlib import metadata

import factory
from factory.django import DjangoModelFactory

from roscoe.projects.tests.factories import ProjectFactory
from roscoe.submissions.tests.factories import SubmissionFactory
from roscoe.users.tests.factories import OrganizationFactory
from roscoe.validations.constants import (
    RulesetType,
    Severity,
    StepStatus,
    ValidationRunStatus,
    ValidationType,
)
from roscoe.validations.models import (
    Artifact,
    Ruleset,
    ValidationFinding,
    ValidationRun,
    ValidationStepRun,
    Validator,
)
from roscoe.workflows.tests.factories import WorkflowFactory, WorkflowStepFactory


class RulesetFactory(DjangoModelFactory):
    class Meta:
        model = Ruleset

    org = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Test Ruleset {n}")
    ruleset_type = RulesetType.JSON_SCHEMA
    version = "1"

    # We don't need to define schema_type.
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

    validation_run = factory.SubFactory(ValidationRunFactory)
    workflow_step = factory.SubFactory(WorkflowStepFactory)
    step_order = factory.Sequence(lambda n: n * 10)
    status = StepStatus.PENDING
    summary = factory.Faker("sentence")


class ValidationFindingFactory(DjangoModelFactory):
    class Meta:
        model = ValidationFinding

    step_run = factory.SubFactory(ValidationStepRunFactory)
    severity = Severity.ERROR
    message = factory.Faker("sentence")
    path = factory.Faker("file_path", depth=3)
    meta = factory.Dict({})


class ArtifactFactory(DjangoModelFactory):
    class Meta:
        model = Artifact

    step_run = factory.SubFactory(ValidationStepRunFactory)
    org = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"artifact-{n}.txt")
    description = factory.Faker("sentence")
    # Note: file field is optional for testing

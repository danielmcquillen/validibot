from importlib import metadata

import factory
from factory.django import DjangoModelFactory

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import StepStatus
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.models import Artifact
from simplevalidations.validations.models import Ruleset
from simplevalidations.validations.models import ValidationFinding
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.models import ValidationStepRun
from simplevalidations.validations.models import Validator
from simplevalidations.workflows.tests.factories import WorkflowFactory
from simplevalidations.workflows.tests.factories import WorkflowStepFactory


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
    output = factory.Dict({})


class ValidationFindingFactory(DjangoModelFactory):
    class Meta:
        model = ValidationFinding

    step_run = factory.SubFactory(ValidationStepRunFactory)
    # Ensure run FK matches the step_run's validation_run
    run = factory.LazyAttribute(lambda o: o.step_run.validation_run)
    severity = Severity.ERROR
    code = ""
    message = factory.Faker("sentence")
    path = factory.Faker("file_path", depth=3)
    meta = factory.Dict({})


class ArtifactFactory(DjangoModelFactory):
    class Meta:
        model = Artifact

    validation_run = factory.SubFactory(ValidationRunFactory)
    org = factory.SubFactory(OrganizationFactory)
    label = factory.Sequence(lambda n: f"artifact-{n}.txt")
    content_type = "text/plain"
    size_bytes = 0
    # file is optional; tests can attach a ContentFile if needed

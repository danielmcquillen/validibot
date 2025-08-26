import factory
from factory.django import DjangoModelFactory

from roscoe.users.tests.factories import OrganizationFactory
from roscoe.users.tests.factories import UserFactory
from roscoe.validations.constants import RulesetType
from roscoe.validations.constants import ValidationType
from roscoe.validations.models import Ruleset
from roscoe.validations.models import Validator
from roscoe.workflows.models import Workflow
from roscoe.workflows.models import WorkflowStep


class RulesetFactory(DjangoModelFactory):
    class Meta:
        model = Ruleset

    name = factory.Sequence(lambda n: f"Test Ruleset {n}")
    slug = factory.Sequence(lambda n: f"test-ruleset-{n}")
    version = 1
    type = RulesetType.JSON_SCHEMA
    org = factory.SubFactory(OrganizationFactory)
    content = factory.LazyFunction(lambda: {"type": "object", "properties": {}})


class ValidatorFactory(DjangoModelFactory):
    class Meta:
        model = Validator

    name = factory.Sequence(lambda n: f"Test Validator {n}")
    slug = factory.Sequence(lambda n: f"test-validator-{n}")
    version = 1
    type = ValidationType.JSON_SCHEMA
    default_ruleset = factory.SubFactory(RulesetFactory)
    description = factory.Faker("text", max_nb_chars=200)
    timeout_seconds = 300


class WorkflowFactory(DjangoModelFactory):
    class Meta:
        model = Workflow

    name = factory.Sequence(lambda n: f"Test Workflow {n}")
    slug = factory.Sequence(lambda n: f"test-workflow-{n}")
    version = 1
    org = factory.SubFactory(OrganizationFactory)
    user = factory.SubFactory(UserFactory)
    is_locked = False


class WorkflowStepFactory(DjangoModelFactory):
    class Meta:
        model = WorkflowStep

    workflow = factory.SubFactory(WorkflowFactory)
    validator = factory.SubFactory(ValidatorFactory)
    order = factory.Sequence(lambda n: n * 10)  # 10, 20, 30, etc.
    name = factory.Sequence(lambda n: f"Step {n}")
    is_enabled = True
    continue_on_failure = False

import factory
from factory.django import DjangoModelFactory

from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.models import WorkflowStep


class WorkflowFactory(DjangoModelFactory):
    class Meta:
        model = Workflow

    org = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Test Workflow {n}")
    uuid = factory.Faker("uuid4")
    slug = factory.Sequence(lambda n: f"test-workflow-{n}")
    # project = factory.SubFactory("simplevalidations.projects.tests.factories.ProjectFactory")
    version = "1"
    is_locked = False


class WorkflowStepFactory(DjangoModelFactory):
    class Meta:
        model = WorkflowStep

    workflow = factory.SubFactory(WorkflowFactory)
    order = factory.Sequence(lambda n: n * 10)  # 10, 20, 30, etc.
    # Avoid circular import by referencing the validator factory via dotted path
    validator = factory.SubFactory(
        "simplevalidations.validations.tests.factories.ValidatorFactory",
    )
    name = factory.Sequence(lambda n: f"Step {n}")
    config = {}

import contextlib

import factory
from factory.django import DjangoModelFactory

from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.users.models import Membership
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.models import WorkflowStep


class WorkflowFactory(DjangoModelFactory):
    class Meta:
        model = Workflow

    class Params:
        with_owner = False  # opt-in helper for tests that need owner permissions

    org = factory.SubFactory(OrganizationFactory)
    user = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Test Workflow {n}")
    uuid = factory.Faker("uuid4")
    slug = factory.Sequence(lambda n: f"test-workflow-{n}")
    project = factory.SubFactory(ProjectFactory, org=factory.SelfAttribute("..org"))
    version = "1"
    is_locked = False
    is_active = True
    allow_submission_meta_data = True
    allow_submission_short_description = True
    allow_submission_name = True
    allowed_file_types = factory.LazyFunction(
        lambda: [SubmissionFileType.JSON],
    )

    @factory.post_generation
    def link_user(self, create, extracted, **kwargs):
        if not create:
            return
        # Ensure the workflow's user has an active membership in the workflow org
        Membership.objects.get_or_create(
            user=self.user,
            org=self.org,
            defaults={"is_active": True},
        )
        if getattr(self, "with_owner", False):
            with contextlib.suppress(ValueError):
                # Tests may override membership manually; skip when access not allowed.
                self.user.set_current_org(self.org)


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
    description = ""
    notes = ""
    display_schema = False
    config = {}

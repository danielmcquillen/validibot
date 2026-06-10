import contextlib
from uuid import uuid4

import factory
from factory.django import DjangoModelFactory

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.models import WorkflowStepResource


class WorkflowFactory(DjangoModelFactory):
    class Meta:
        model = Workflow
        skip_postgeneration_save = True

    org = factory.SubFactory(OrganizationFactory)
    user = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Test Workflow {n}")
    uuid = factory.Faker("uuid4")
    slug = factory.LazyFunction(lambda: f"test-workflow-{uuid4().hex[:12]}")
    project = factory.SubFactory(ProjectFactory, org=factory.SelfAttribute("..org"))
    version = 1
    history_policy = WorkflowHistoryPolicy.VERSIONED
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

    @factory.post_generation
    def with_owner(self, create, extracted, **kwargs):
        """Optionally grant the workflow user owner access to its organization."""
        if not create or not extracted:
            return
        membership, _created = Membership.objects.get_or_create(
            user=self.user,
            org=self.org,
            defaults={"is_active": True},
        )
        membership.add_role(RoleCode.OWNER)
        with contextlib.suppress(ValueError):
            # Make current-org permission helpers agree with object-scoped checks.
            self.user.set_current_org(self.org)


class WorkflowStepFactory(DjangoModelFactory):
    class Meta:
        model = WorkflowStep

    workflow = factory.SubFactory(WorkflowFactory)
    order = factory.Sequence(lambda n: n * 10)  # 10, 20, 30, etc.
    # Avoid circular import by referencing the validator factory via dotted path
    validator = factory.SubFactory(
        "validibot.validations.tests.factories.ValidatorFactory",
    )
    name = factory.Sequence(lambda n: f"Step {n}")
    description = ""
    notes = ""
    display_schema = False
    config = {}


class WorkflowStepResourceFactory(DjangoModelFactory):
    """Factory for WorkflowStepResource — catalog reference mode by default.

    For step-owned files, pass ``validator_resource_file=None`` and provide
    ``step_resource_file`` and ``filename``.
    """

    class Meta:
        model = WorkflowStepResource

    step = factory.SubFactory(WorkflowStepFactory)
    role = WorkflowStepResource.WEATHER_FILE
    # Default: catalog reference pointing to a ValidatorResourceFile
    validator_resource_file = factory.SubFactory(
        "validibot.validations.tests.factories.ValidatorResourceFileFactory",
    )

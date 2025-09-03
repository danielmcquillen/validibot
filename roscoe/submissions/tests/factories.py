import factory
from factory.django import DjangoModelFactory

from roscoe.projects.tests.factories import ProjectFactory
from roscoe.submissions.models import Submission
from roscoe.users.tests.factories import OrganizationFactory
from roscoe.users.tests.factories import UserFactory


class SubmissionFactory(DjangoModelFactory):
    class Meta:
        model = Submission

    name = factory.Sequence(lambda n: f"Test Submission {n}")
    org = factory.SubFactory(OrganizationFactory)
    project = factory.SubFactory(ProjectFactory)
    user = factory.SubFactory(UserFactory)

    # Add workflow if it's required
    @factory.lazy_attribute
    def workflow(self):
        # Import here to avoid circular imports
        from roscoe.workflows.tests.factories import WorkflowFactory

        return WorkflowFactory(org=self.org, user=self.user)

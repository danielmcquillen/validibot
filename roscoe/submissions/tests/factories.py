import factory
from factory.django import DjangoModelFactory

from roscoe.projects.tests.factories import ProjectFactory
from roscoe.submissions.constants import SubmissionFileType
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
    content = "{}"  # non-empty text
    file_type = SubmissionFileType.JSON  # matches the document

    # Add workflow if it's required
    @factory.lazy_attribute
    def workflow(self):
        # Import here to avoid circular imports
        from roscoe.workflows.tests.factories import WorkflowFactory  # noqa: PLC0415

        return WorkflowFactory(org=self.org, user=self.user)

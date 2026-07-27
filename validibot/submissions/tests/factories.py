import factory
from factory.django import DjangoModelFactory

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.models import Submission
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory


class SubmissionFactory(DjangoModelFactory):
    """Create submissions whose workflow, project, and org agree by default."""

    class Meta:
        model = Submission

    @classmethod
    def _generate(cls, strategy, params):
        """Propagate explicitly supplied parents through the generated graph."""

        params = dict(params)
        workflow = params.get("workflow")
        project = params.get("project")
        org = params.get("org")
        user = params.get("user")

        if workflow is not None:
            params.setdefault("org", workflow.org)
            params.setdefault("project", workflow.project)
            params.setdefault("user", workflow.user)
        elif project is not None:
            params.setdefault("org", project.org)
            params.setdefault("workflow__org", project.org)
            params.setdefault("workflow__project", project)

        if org is not None:
            params.setdefault("project__org", org)
            params.setdefault("workflow__org", org)
        if user is not None:
            params.setdefault("workflow__user", user)

        return super()._generate(strategy, params)

    name = factory.Sequence(lambda n: f"Test Submission {n}")
    org = factory.SubFactory(OrganizationFactory)
    user = factory.SubFactory(UserFactory)
    project = factory.SubFactory(
        ProjectFactory,
        org=factory.SelfAttribute("..org"),
    )
    workflow = factory.SubFactory(
        "validibot.workflows.tests.factories.WorkflowFactory",
        org=factory.SelfAttribute("..org"),
        project=factory.SelfAttribute("..project"),
        user=factory.SelfAttribute("..user"),
    )
    content = "{}"  # non-empty text
    file_type = SubmissionFileType.JSON  # matches the document

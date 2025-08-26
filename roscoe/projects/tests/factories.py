import factory
from factory.django import DjangoModelFactory

from roscoe.projects.models import Project
from roscoe.users.tests.factories import OrganizationFactory


class ProjectFactory(DjangoModelFactory):
    class Meta:
        model = Project

    name = factory.Sequence(lambda n: f"Test Project {n}")
    slug = factory.Sequence(lambda n: f"test-project-{n}")
    org = factory.SubFactory(OrganizationFactory)
    description = factory.Faker("text", max_nb_chars=200)

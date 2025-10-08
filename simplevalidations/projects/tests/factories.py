import factory
from factory.django import DjangoModelFactory

from simplevalidations.projects.models import Project
from simplevalidations.users.tests.factories import OrganizationFactory


class ProjectFactory(DjangoModelFactory):
    class Meta:
        model = Project

    name = factory.Sequence(lambda n: f"Test Project {n}")
    slug = factory.Sequence(lambda n: f"test-project-{n}")
    org = factory.SubFactory(OrganizationFactory)
    description = factory.Faker("text", max_nb_chars=200)
    is_default = False
    is_active = True
    color = "#6C757D"

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        return Project.all_objects.create(*args, **kwargs)

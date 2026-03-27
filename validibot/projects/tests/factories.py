from uuid import uuid4

import factory
from factory.django import DjangoModelFactory

from validibot.projects.models import Project
from validibot.users.tests.factories import OrganizationFactory


class ProjectFactory(DjangoModelFactory):
    class Meta:
        model = Project

    name = factory.Sequence(lambda n: f"Test Project {n}")
    slug = factory.LazyFunction(lambda: f"test-project-{uuid4().hex[:12]}")
    org = factory.SubFactory(OrganizationFactory)
    description = factory.Faker("text", max_nb_chars=200)
    is_default = False
    is_active = True
    color = "#6C757D"

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        return Project.all_objects.create(*args, **kwargs)

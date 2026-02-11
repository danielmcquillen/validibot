import factory
from factory.django import DjangoModelFactory

from validibot.events.constants import AppEventType
from validibot.projects.tests.factories import ProjectFactory
from validibot.tracking.constants import TrackingEventType
from validibot.tracking.models import TrackingEvent
from validibot.users.tests.factories import UserFactory


class TrackingEventFactory(DjangoModelFactory):
    class Meta:
        model = TrackingEvent

    project = factory.SubFactory(ProjectFactory)
    org = factory.LazyAttribute(lambda obj: obj.project.org)

    @factory.lazy_attribute
    def user(self):
        return UserFactory(orgs=[self.org])

    event_type = TrackingEventType.APP_EVENT
    app_event_type = AppEventType.VALIDATION_RUN_CREATED
    extra_data = factory.Dict({})

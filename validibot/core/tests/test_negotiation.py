import pytest
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from validibot.core.api.negotiation import AgentAwareNegotiation


@pytest.mark.parametrize(
    ("request_kwargs", "expected_profile"),
    [
        ({"HTTP_ACCEPT": "application/vnd.validibot.agent+json"}, "agent"),
        ({"HTTP_ACCEPT": "application/json", "HTTP_X_AGENT_PROFILE": "a2a"}, "a2a"),
        ({"HTTP_ACCEPT": "application/json"}, "human"),
    ],
)
def test_agent_profile_assignment(request_kwargs, expected_profile):
    negotiation = AgentAwareNegotiation()
    factory = APIRequestFactory()
    request = factory.get("/example/", **request_kwargs)
    drf_request = Request(request)

    renderer = JSONRenderer()
    selected_renderer, media_type = negotiation.select_renderer(drf_request, [renderer])

    assert drf_request.agent_profile == expected_profile
    assert selected_renderer is renderer
    assert media_type == renderer.media_type

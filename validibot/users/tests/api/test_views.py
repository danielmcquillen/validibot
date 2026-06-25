import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.test import APIRequestFactory

from validibot.users.api.views import UserViewSet
from validibot.users.models import User


class TestUserViewSet:
    """
    Tests for the UserViewSet API.

    The UserViewSet only exposes the 'me' action to return the
    current user. List, retrieve, and update operations are not
    available via API — the API surface is intentionally minimal.
    """

    @pytest.fixture
    def api_rf(self) -> APIRequestFactory:
        return APIRequestFactory()

    @pytest.fixture
    def api_client(self) -> APIClient:
        """A DRF test client that exercises the full request stack.

        Unlike ``api_rf`` (which builds a bare request for calling the view
        method directly), ``APIClient`` routes through URL resolution,
        authentication, and permission checks — exactly what we need to prove
        the endpoint is *gated*, not merely that the action returns data.
        """
        return APIClient()

    def test_me(self, user: User, api_rf: APIRequestFactory):
        """Test that the me action returns the current user's details."""
        view = UserViewSet()
        request = api_rf.get("/fake-url/")
        request.user = user

        view.request = request

        response = view.me(request)  # type: ignore[call-arg, arg-type, misc]

        assert response.data == {
            "username": user.username,
            "name": user.name,
        }

    # ── Authentication gate ──────────────────────────────────────────────
    # ``/users/me/`` declares ``permission_classes = [IsAuthenticated]``. These
    # tests go through the full request stack (URL → auth → permission) to prove
    # the gate actually rejects anonymous callers. The unit ``test_me`` above
    # bypasses this layer by calling the action directly, so without these the
    # suite would pass even if the endpoint were accidentally left public.

    @pytest.mark.django_db
    def test_me_requires_authentication(self, api_client: APIClient):
        """An anonymous request to /users/me/ must be denied, never 200.

        Why it matters: this is the regression guard against the endpoint
        silently opening up — e.g. if the global ``DEFAULT_PERMISSION_CLASSES``
        changed or ``DRF_ALLOW_ANONYMOUS`` were flipped. DRF returns 403 here
        (not 401) because ``SessionAuthentication`` is the first authenticator
        and supplies no ``WWW-Authenticate`` header; we accept either code so
        the test asserts the security property (access denied) without being
        brittle to authenticator ordering.
        """
        url = reverse("api:user-me")

        response = api_client.get(url)

        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )

    @pytest.mark.django_db
    def test_me_returns_current_user_when_authenticated(
        self,
        api_client: APIClient,
        user: User,
    ):
        """An authenticated request returns *only* the caller's own profile.

        Why it matters: confirms the explicit ``permission_classes`` change did
        not break the happy path, and that the response is scoped to the
        requesting user (username + name only) rather than leaking other users.
        """
        api_client.force_authenticate(user=user)
        url = reverse("api:user-me")

        response = api_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"username": user.username, "name": user.name}

    # ── Read-only surface ────────────────────────────────────────────────
    # ``http_method_names`` is restricted to the safe verbs and the router maps
    # ``/users/me/`` to the GET-only ``me`` action. Write attempts must be
    # rejected as 405. We authenticate first so the check exercises the method
    # gate rather than the auth gate (which would short-circuit to 403).

    @pytest.mark.django_db
    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_me_rejects_write_methods(
        self,
        api_client: APIClient,
        user: User,
        method: str,
    ):
        """Write verbs against /users/me/ are rejected (read-only endpoint).

        Why it matters: the user API is intentionally read-only — all user
        mutation goes through the Django admin. This locks in that contract so a
        future write action can't be bolted onto this route without a failing
        test flagging it.
        """
        api_client.force_authenticate(user=user)
        url = reverse("api:user-me")

        response = getattr(api_client, method)(url)

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

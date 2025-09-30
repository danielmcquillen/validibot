from django.urls import resolve
from django.urls import reverse

from simplevalidations.users.models import User


def test_detail(user: User):
    assert (
        reverse("users:detail", kwargs={"username": user.username})
        == f"/app/users/{user.username}/"
    )
    assert resolve(f"/app/users/{user.username}/").view_name == "users:detail"


def test_update():
    assert reverse("users:update") == "/app/users/~update/"
    assert resolve("/app/users/~update/").view_name == "users:update"


def test_redirect():
    assert reverse("users:redirect") == "/app/users/~redirect/"
    assert resolve("/app/users/~redirect/").view_name == "users:redirect"

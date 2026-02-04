from django.urls import resolve
from django.urls import reverse

from validibot.users.models import User


def test_detail(user: User):
    assert (
        reverse("users:detail", kwargs={"username": user.username})
        == f"/app/users/{user.username}/"
    )
    assert resolve(f"/app/users/{user.username}/").view_name == "users:detail"


def test_profile():
    assert reverse("users:profile") == "/app/users/profile/"
    assert resolve("/app/users/profile/").view_name == "users:profile"


def test_redirect():
    assert reverse("users:redirect") == "/app/users/~redirect/"
    assert resolve("/app/users/~redirect/").view_name == "users:redirect"


def test_email_url():
    assert reverse("users:email") == "/app/users/email/"
    assert resolve("/app/users/email/").view_name == "users:email"


def test_api_key_url():
    assert reverse("users:api-key") == "/app/users/api-key/"
    assert resolve("/app/users/api-key/").view_name == "users:api-key"

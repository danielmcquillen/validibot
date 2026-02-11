from http import HTTPStatus

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse

from validibot.core.models import SupportMessage


@pytest.mark.django_db
def test_support_message_creates_message_and_sends_email(client, settings):
    """Verify support message is created and admin email is sent."""
    settings.ADMINS = ["support@example.com"]
    user = get_user_model().objects.create_user(
        username="supporter",
        email="supporter@example.com",
        password="StrongPass!23",  # noqa: S106
    )
    client.force_login(user)

    response = client.post(
        reverse("core:support_message_create"),
        data={
            "subject": "Need integration guidance",
            "message": "Could you send the setup checklist?",
        },
    )

    # View redirects after successful submission
    assert response.status_code == HTTPStatus.FOUND
    saved_message = SupportMessage.objects.get(user=user)
    assert saved_message.subject == "Need integration guidance"
    assert len(mail.outbox) == 1
    assert "Need integration guidance" in mail.outbox[0].body


@pytest.mark.django_db
def test_support_message_invalid_redirects(client):
    """Verify invalid form data redirects with error message."""
    user = get_user_model().objects.create_user(
        username="invalid_tester",
        email="invalid@example.com",
        password="StrongPass!23",  # noqa: S106
    )
    client.force_login(user)

    response = client.post(
        reverse("core:support_message_create"),
        data={"subject": "   ", "message": "   "},
    )

    # View redirects on validation error
    assert response.status_code == HTTPStatus.FOUND
    # No message should be saved
    assert SupportMessage.objects.count() == 0

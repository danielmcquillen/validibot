from http import HTTPStatus

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.urls import reverse

from simplevalidations.core.models import SupportMessage


@pytest.mark.django_db
def test_support_message_htmx_success(client, settings):
    settings.ADMINS = [("Support", "support@example.com")]
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
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.CREATED
    assert "Message received" in response.content.decode()
    saved_message = SupportMessage.objects.get(user=user)
    assert saved_message.subject == "Need integration guidance"
    assert len(mail.outbox) == 1
    assert "Need integration guidance" in mail.outbox[0].body


@pytest.mark.django_db
def test_support_message_htmx_invalid_returns_form_with_errors(client):
    user = get_user_model().objects.create_user(
        username="invalid_tester",
        email="invalid@example.com",
        password="StrongPass!23",  # noqa: S106
    )
    client.force_login(user)

    response = client.post(
        reverse("core:support_message_create"),
        data={"subject": "   ", "message": "   "},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.context is not None
    form = response.context["form"]
    assert form.errors["subject"] == ["Please add a little more detail."]
    assert form.errors["message"] == ["Please add a little more detail."]
    assert "is-invalid" in response.content.decode()

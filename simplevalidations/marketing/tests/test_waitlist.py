from __future__ import annotations

import pytest
from django.core import mail
from django.urls import reverse

from simplevalidations.marketing.forms import BetaWaitlistForm
from simplevalidations.marketing.models import Prospect


@pytest.mark.django_db
def test_waitlist_signup_success_htmx_saves_prospect_and_sends_email(client):
    mail.outbox.clear()

    response = client.post(
        reverse("marketing:beta_waitlist"),
        data={"email": "person@company.com", "company": ""},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 201
    body = response.content.decode()
    assert "beta is ready" in body

    prospect = Prospect.objects.get(email="person@company.com")
    assert prospect.origin == Prospect.Origins.HERO
    assert prospect.source == "marketing_homepage"
    assert prospect.welcome_sent_at is not None

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert "SimpleValidations" in message.subject
    assert message.to == ["person@company.com"]


@pytest.mark.django_db
def test_waitlist_footer_flow_returns_tersed_message(client):
    mail.outbox.clear()

    response = client.post(
        reverse("marketing:beta_waitlist"),
        data={
            "email": "footer@company.com",
            "company": "",
            "origin": BetaWaitlistForm.ORIGIN_FOOTER,
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 201
    assert "Thanks! We&#x27;ll be in touch soon." in response.content.decode()

    prospect = Prospect.objects.get(email="footer@company.com")
    assert prospect.origin == Prospect.Origins.FOOTER
    assert prospect.source == "marketing_footer"

    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_waitlist_failure_surfaces_form_error(client, monkeypatch):
    mail.outbox.clear()

    def boom(*args, **kwargs):
        raise Exception("Postmark error")

    monkeypatch.setattr("simplevalidations.marketing.services.send_mail", boom)

    response = client.post(
        reverse("marketing:beta_waitlist"),
        data={"email": "person@company.com", "company": ""},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 400
    assert "Please try again in a moment." in response.content.decode()
    assert Prospect.objects.filter(email="person@company.com").exists()


@pytest.mark.django_db
def test_waitlist_honeypot_blocks_bot_submission(client):
    mail.outbox.clear()

    response = client.post(
        reverse("marketing:beta_waitlist"),
        data={"email": "person@company.com", "company": "ACME"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 400
    assert "hidden field blank" in response.content.decode()
    assert Prospect.objects.count() == 0

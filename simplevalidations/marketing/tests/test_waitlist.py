from __future__ import annotations

import json
from http import HTTPStatus

import pytest
from django.core import mail
from django.urls import reverse

from simplevalidations.marketing.constants import ProspectEmailStatus
from simplevalidations.marketing.constants import ProspectOrigins
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

    assert response.status_code == HTTPStatus.CREATED
    body = response.content.decode()
    assert "beta is ready" in body

    prospect = Prospect.objects.get(email="person@company.com")
    assert prospect.origin == ProspectOrigins.HERO
    assert prospect.source == "marketing_homepage"
    assert prospect.email_status == ProspectEmailStatus.PENDING
    assert prospect.welcome_sent_at is not None

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert "Validibot" in message.subject
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

    assert response.status_code == HTTPStatus.CREATED
    assert "Thanks! We&#x27;ll be in touch soon." in response.content.decode()

    prospect = Prospect.objects.get(email="footer@company.com")
    assert prospect.origin == ProspectOrigins.FOOTER
    assert prospect.source == "marketing_footer"
    assert prospect.email_status == ProspectEmailStatus.PENDING

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

    assert response.status_code == HTTPStatus.BAD_REQUEST
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

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "hidden field blank" in response.content.decode()
    assert Prospect.objects.count() == 0


@pytest.mark.django_db
def test_postmark_delivery_webhook_marks_prospect_verified(client):
    mail.outbox.clear()

    client.post(
        reverse("marketing:beta_waitlist"),
        data={"email": "person@company.com", "company": ""},
        HTTP_HX_REQUEST="true",
    )

    response = client.post(
        reverse("marketing:postmark_delivery_webhook"),
        data=json.dumps({"RecordType": "Delivery", "Recipient": "person@company.com"}),
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="3.134.147.250",
        REMOTE_ADDR="3.134.147.250",
    )

    assert response.status_code == HTTPStatus.OK
    prospect = Prospect.objects.get(email="person@company.com")
    assert prospect.email_status == ProspectEmailStatus.VERIFIED


@pytest.mark.django_db
def test_postmark_bounce_webhook_marks_prospect_invalid(client):
    mail.outbox.clear()

    client.post(
        reverse("marketing:beta_waitlist"),
        data={"email": "person@company.com", "company": ""},
        HTTP_HX_REQUEST="true",
    )

    response = client.post(
        reverse("marketing:postmark_bounce_webhook"),
        data=json.dumps(
            {
                "RecordType": "Bounce",
                "Type": "HardBounce",
                "Email": "person@company.com",
            }
        ),
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="3.134.147.250",
        REMOTE_ADDR="3.134.147.250",
    )

    assert response.status_code == HTTPStatus.OK
    prospect = Prospect.objects.get(email="person@company.com")
    assert prospect.email_status == ProspectEmailStatus.INVALID


@pytest.mark.django_db
def test_postmark_webhook_rejects_unknown_ip(client):
    mail.outbox.clear()

    client.post(
        reverse("marketing:beta_waitlist"),
        data={"email": "person@company.com", "company": ""},
        HTTP_HX_REQUEST="true",
    )

    response = client.post(
        reverse("marketing:postmark_delivery_webhook"),
        data=json.dumps({"RecordType": "Delivery", "Recipient": "person@company.com"}),
        content_type="application/json",
        HTTP_X_FORWARDED_FOR="1.1.1.1",
        REMOTE_ADDR="1.1.1.1",
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    prospect = Prospect.objects.get(email="person@company.com")
    assert prospect.email_status == ProspectEmailStatus.PENDING

import base64
import hashlib
import hmac
import json

import pytest
from django.test import override_settings
from django.urls import reverse


def _signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


@override_settings(
    POSTMARK_WEBHOOK_SIGNING_SECRET="super-secret",
    POSTMARK_WEBHOOK_ALLOWED_IPS=[],
)
@pytest.mark.django_db()
def test_postmark_signature_required_and_valid(client):
    payload = {"RecordType": "Delivery", "Recipient": "test@example.com"}
    body = json.dumps(payload).encode("utf-8")
    url = reverse("marketing:postmark_delivery_webhook")
    sig = _signature(body, "super-secret")

    resp = client.post(
        url,
        data=body,
        content_type="application/json",
        HTTP_X_POSTMARK_SIGNATURE=sig,
        REMOTE_ADDR="203.0.113.10",
    )

    assert resp.status_code == 200


@override_settings(
    POSTMARK_WEBHOOK_SIGNING_SECRET="super-secret",
    POSTMARK_WEBHOOK_ALLOWED_IPS=[],
)
@pytest.mark.django_db()
def test_postmark_rejects_invalid_signature(client):
    payload = {"RecordType": "Delivery", "Recipient": "test@example.com"}
    body = json.dumps(payload).encode("utf-8")
    url = reverse("marketing:postmark_delivery_webhook")

    resp = client.post(
        url,
        data=body,
        content_type="application/json",
        HTTP_X_POSTMARK_SIGNATURE="invalid",
        REMOTE_ADDR="203.0.113.10",
    )

    assert resp.status_code == 403


@override_settings(
    POSTMARK_WEBHOOK_SIGNING_SECRET="",
    POSTMARK_WEBHOOK_ALLOWED_IPS=["198.51.100.1"],
)
@pytest.mark.django_db()
def test_postmark_ignores_spoofed_forwarded_ip(client):
    payload = {"RecordType": "Delivery", "Recipient": "test@example.com"}
    body = json.dumps(payload).encode("utf-8")
    url = reverse("marketing:postmark_delivery_webhook")

    resp = client.post(
        url,
        data=body,
        content_type="application/json",
        REMOTE_ADDR="203.0.113.10",
        HTTP_X_FORWARDED_FOR="198.51.100.1",
    )

    assert resp.status_code == 403


@override_settings(
    POSTMARK_WEBHOOK_SIGNING_SECRET="",
    POSTMARK_WEBHOOK_ALLOWED_IPS=["198.51.100.1"],
)
@pytest.mark.django_db()
def test_postmark_allows_request_from_allowed_remote_addr(client):
    payload = {"RecordType": "Delivery", "Recipient": "test@example.com"}
    body = json.dumps(payload).encode("utf-8")
    url = reverse("marketing:postmark_delivery_webhook")

    resp = client.post(
        url,
        data=body,
        content_type="application/json",
        REMOTE_ADDR="198.51.100.1",
    )

    assert resp.status_code == 200

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.tests.factories import OrganizationFactory, UserFactory, grant_role
from simplevalidations.validations.services.fmi import create_fmi_validator
from simplevalidations.validations.tests.test_fmi_engine import _fake_fmu  # reuse helper


class FMIProbeViewTests(TestCase):
    """Exercise the HTMX probe start/status endpoints."""

    def setUp(self):
        self.org = OrganizationFactory()
        self.user = UserFactory(orgs=[self.org])
        grant_role(self.user, self.org, RoleCode.OWNER)
        self.client.force_login(self.user)
        self.user.set_current_org(self.org)
        self.validator = create_fmi_validator(
            org=self.org,
            project=None,
            name="Probe Validator",
            upload=_fake_fmu(),
        )

    def test_probe_start_returns_queue_status(self):
        url = reverse("validations:fmi_probe_start", args=[self.validator.pk])
        response = self.client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"status": "queued"})

    def test_probe_status_returns_data(self):
        url = reverse("validations:fmi_probe_status", args=[self.validator.pk])
        response = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("status", data)
        self.assertIn("details", data)

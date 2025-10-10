import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from simplevalidations.marketing.forms import BetaWaitlistForm
from simplevalidations.marketing.models import Prospect


class MarketingMetadataTests(TestCase):
    def test_homepage_includes_serializable_structured_data(self):
        response = self.client.get(reverse("marketing:home"))
        self.assertEqual(response.status_code, 200)

        structured_data = response.context.get("structured_data_json")
        self.assertIsInstance(structured_data, str)

        payload = json.loads(structured_data)
        self.assertIsInstance(payload, list)
        self.assertTrue(any(item.get("@type") == "WebPage" for item in payload))
        self.assertTrue(any(item.get("@type") == "Organization" for item in payload))

    def test_meta_fields_resolved_to_plain_strings(self):
        response = self.client.get(reverse("marketing:home"))
        self.assertEqual(response.status_code, 200)

        self.assertIsInstance(response.context.get("meta_description"), str)
        self.assertIsInstance(response.context.get("meta_keywords"), str)


class MarketingWaitlistTests(TestCase):
    def setUp(self):
        self.url = reverse("marketing:beta_waitlist")
        self.hx_headers = {"HTTP_HX_REQUEST": "true"}

    @patch("simplevalidations.marketing.views.submit_waitlist_signup")
    def test_new_signup_returns_created_status(self, mock_submit):
        response = self.client.post(
            self.url,
            {
                "email": "new@example.com",
                "origin": BetaWaitlistForm.ORIGIN_HERO,
            },
            **self.hx_headers,
        )
        self.assertEqual(response.status_code, 201)
        mock_submit.assert_called_once()
        payload = mock_submit.call_args.args[0]
        self.assertNotIn("skip_email", payload.metadata)

    @patch("simplevalidations.marketing.views.submit_waitlist_signup")
    def test_duplicate_signup_skips_email_and_returns_message(self, mock_submit):
        Prospect.objects.create(email="dup@example.com")

        response = self.client.post(
            self.url,
            {
                "email": "dup@example.com",
                "origin": BetaWaitlistForm.ORIGIN_HERO,
            },
            **self.hx_headers,
        )
        self.assertEqual(response.status_code, 200)
        mock_submit.assert_called_once()
        payload = mock_submit.call_args.args[0]
        self.assertTrue(payload.metadata.get("skip_email"))
        self.assertIn(
            "already on the beta list",
            response.content.decode("utf-8").lower(),
        )

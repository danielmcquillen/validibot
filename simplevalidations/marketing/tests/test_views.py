import json

from django.test import TestCase
from django.urls import reverse


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

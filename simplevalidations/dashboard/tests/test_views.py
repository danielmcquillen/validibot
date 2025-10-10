from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from simplevalidations.submissions.tests.factories import SubmissionFactory
from simplevalidations.tracking.tests.factories import TrackingEventFactory
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.validations.tests.factories import ValidationFindingFactory
from simplevalidations.validations.tests.factories import ValidationRunFactory
from simplevalidations.validations.tests.factories import ValidationStepRunFactory


class DashboardViewTests(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.org = self.user.orgs.first()
        self.user.set_current_org(self.org)
        self.client.force_login(self.user)

    def test_dashboard_page_renders_with_widgets(self):
        response = self.client.get(reverse("dashboard:my_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "dashboard/my_dashboard.html")
        widget_definitions = response.context["widget_definitions"]
        self.assertGreater(len(list(widget_definitions)), 0)

    def _create_run_for_org(self, *, hours_ago: int = 1):
        submission = SubmissionFactory(
            org=self.org,
            user=self.user,
            project__org=self.org,
        )
        run = ValidationRunFactory(submission=submission)
        run.created = timezone.now() - timedelta(hours=hours_ago)
        run.save(update_fields=["created"])
        return run

    def test_total_validations_widget_counts_scoped_runs(self):
        run = self._create_run_for_org(hours_ago=2)
        other_org = OrganizationFactory()
        other_submission = SubmissionFactory(org=other_org, project__org=other_org)
        other_run = ValidationRunFactory(submission=other_submission)
        response = self.client.get(
            reverse("dashboard:widget-detail", kwargs={"slug": "total-validations"}),
            {"time_range": "24h"},
        )
        response.render()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context_data["total_count"], 1)
        self.assertNotIn(str(other_run.pk), response.content.decode())

    def test_total_errors_widget_counts_error_findings(self):
        run = self._create_run_for_org(hours_ago=3)
        step_run = ValidationStepRunFactory(validation_run=run)
        finding = ValidationFindingFactory(
            validation_step_run=step_run,
            validation_run=run,
        )
        finding.created = timezone.now() - timedelta(hours=1)
        finding.save(update_fields=["created"])

        response = self.client.get(
            reverse("dashboard:widget-detail", kwargs={"slug": "total-errors"}),
            {"time_range": "24h"},
        )
        response.render()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context_data["total_count"], 1)

    def test_events_widget_returns_chart_payload(self):
        event = TrackingEventFactory(
            project__org=self.org,
            org=self.org,
            user=self.user,
        )
        event.created = timezone.now() - timedelta(hours=2)
        event.save(update_fields=["created"])

        response = self.client.get(
            reverse("dashboard:widget-detail", kwargs={"slug": "events-time-series"}),
            {"time_range": "24h"},
        )
        response.render()
        config = response.context_data["chart_config"]
        self.assertEqual(config["type"], "line")
        self.assertTrue(config["data"]["datasets"][0]["data"])

    def test_users_widget_counts_distinct_users(self):
        other_user = UserFactory(orgs=[self.org])
        first_event = TrackingEventFactory(
            project__org=self.org,
            org=self.org,
            user=self.user,
        )
        second_event = TrackingEventFactory(
            project__org=self.org,
            org=self.org,
            user=other_user,
        )
        now = timezone.now()
        first_event.created = now - timedelta(hours=2)
        first_event.save(update_fields=["created"])
        second_event.created = now - timedelta(hours=1)
        second_event.save(update_fields=["created"])

        response = self.client.get(
            reverse("dashboard:widget-detail", kwargs={"slug": "users-time-series"}),
            {"time_range": "24h"},
        )
        response.render()
        dataset = response.context_data["chart_config"]["data"]["datasets"][0]["data"]
        self.assertEqual(max(dataset), 1)

"""
Unit tests for dashboard service functions.

These tests verify the core time-series aggregation and chart-building
logic independently of the widget/view layer. The service functions are
the foundation of all dashboard charts — if they produce incorrect data,
every widget is wrong.

Key areas tested:
    - generate_time_series: aggregation, zero-filling, distinct counts,
      empty querysets, bucket granularity
    - build_chart_payload: Chart.js config structure, label formatting
    - build_stacked_bar_payload: multi-series structure, empty input
    - Time range edge cases: single-bucket ranges, boundary alignment
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from validibot.dashboard.services import build_chart_payload
from validibot.dashboard.services import build_stacked_bar_payload
from validibot.dashboard.services import generate_time_series
from validibot.dashboard.time_ranges import resolve_time_range
from validibot.tracking.models import TrackingEvent
from validibot.tracking.tests.factories import TrackingEventFactory
from validibot.users.tests.factories import UserFactory

# ---------------------------------------------------------------------------
# generate_time_series
# ---------------------------------------------------------------------------


class GenerateTimeSeriesTests(TestCase):
    """Tests for the generate_time_series aggregation function.

    This function is the backbone of every dashboard chart. It takes a
    queryset, a time range, and a bucket size, and returns a list of
    (datetime, count) tuples with zero-filled gaps. Getting this wrong
    would make charts show incorrect data or missing periods.
    """

    def setUp(self):
        self.user = UserFactory()
        self.org = self.user.orgs.first()

    def test_empty_queryset_returns_zero_filled_series(self):
        """An empty queryset should still return time buckets, all with value 0.

        This matters because the chart needs x-axis labels even when there's
        no data — otherwise the user sees a blank chart with no indication
        of what time period is shown.
        """
        time_range = resolve_time_range("24h")
        series = generate_time_series(
            TrackingEvent.objects.none(),
            time_range=time_range,
            bucket="hour",
        )
        self.assertGreater(len(series), 0, "Should have zero-filled buckets")
        self.assertTrue(
            all(value == 0 for _, value in series),
            "All values should be 0 for empty queryset",
        )

    def test_hourly_buckets_for_24h_range(self):
        """A 24-hour range with hourly buckets should produce ~24 buckets.

        The exact count depends on the alignment of start/end to hour
        boundaries, but it should be in the 23-25 range.
        """
        time_range = resolve_time_range("24h")
        series = generate_time_series(
            TrackingEvent.objects.none(),
            time_range=time_range,
            bucket="hour",
        )
        self.assertGreaterEqual(len(series), 23)
        self.assertLessEqual(len(series), 25)

    def test_daily_buckets_for_7d_range(self):
        """A 7-day range with daily buckets should produce 7-8 buckets."""
        time_range = resolve_time_range("7d")
        series = generate_time_series(
            TrackingEvent.objects.none(),
            time_range=time_range,
            bucket="day",
        )
        self.assertGreaterEqual(len(series), 7)
        self.assertLessEqual(len(series), 8)

    def test_events_counted_in_correct_bucket(self):
        """Events should be aggregated into the correct time bucket.

        We create two events 3 hours apart and verify they land in
        different hourly buckets with a count of 1 each.
        """
        now = timezone.now()
        e1 = TrackingEventFactory(org=self.org, user=self.user, project__org=self.org)
        e1.created = now - timedelta(hours=2)
        e1.save(update_fields=["created"])

        e2 = TrackingEventFactory(org=self.org, user=self.user, project__org=self.org)
        e2.created = now - timedelta(hours=5)
        e2.save(update_fields=["created"])

        time_range = resolve_time_range("24h")
        series = generate_time_series(
            TrackingEvent.objects.filter(org=self.org),
            time_range=time_range,
            bucket="hour",
        )

        non_zero = [(period, value) for period, value in series if value > 0]
        self.assertEqual(
            len(non_zero),
            2,
            "Two events in different hours should produce two non-zero buckets",
        )
        self.assertTrue(all(v == 1 for _, v in non_zero))

    def test_multiple_events_same_bucket_are_summed(self):
        """Multiple events in the same hour should be summed, not counted as 1."""
        now = timezone.now()
        # Pin to the start of an hour 2 hours ago to ensure all 3 events
        # land in the same hourly bucket regardless of when the test runs.
        base_time = (now - timedelta(hours=2)).replace(
            minute=5, second=0, microsecond=0
        )
        for i in range(3):
            e = TrackingEventFactory(
                org=self.org, user=self.user, project__org=self.org
            )
            e.created = base_time + timedelta(minutes=i * 10)
            e.save(update_fields=["created"])

        time_range = resolve_time_range("24h")
        series = generate_time_series(
            TrackingEvent.objects.filter(org=self.org),
            time_range=time_range,
            bucket="hour",
        )
        non_zero = [v for _, v in series if v > 0]
        self.assertEqual(len(non_zero), 1, "All events in same hour = one bucket")
        self.assertEqual(non_zero[0], 3)

    def test_distinct_count(self):
        """Distinct counts: duplicate user_ids in same bucket counted once.

        This is how the Users widget counts unique users per interval.
        """
        now = timezone.now()
        base_time = now - timedelta(hours=1)
        # Same user, two events in the same hour
        for i in range(2):
            e = TrackingEventFactory(
                org=self.org, user=self.user, project__org=self.org
            )
            e.created = base_time + timedelta(minutes=i * 10)
            e.save(update_fields=["created"])

        time_range = resolve_time_range("24h")
        series = generate_time_series(
            TrackingEvent.objects.filter(org=self.org),
            time_range=time_range,
            bucket="hour",
            value_field="user_id",
            distinct=True,
        )
        non_zero = [v for _, v in series if v > 0]
        self.assertEqual(non_zero[0], 1, "Same user counted once with distinct=True")

    def test_events_outside_range_excluded(self):
        """Events outside the time range should not appear in the series."""
        now = timezone.now()
        e = TrackingEventFactory(org=self.org, user=self.user, project__org=self.org)
        e.created = now - timedelta(days=10)
        e.save(update_fields=["created"])

        time_range = resolve_time_range("24h")
        series = generate_time_series(
            TrackingEvent.objects.filter(org=self.org),
            time_range=time_range,
            bucket="hour",
        )
        self.assertTrue(
            all(value == 0 for _, value in series),
            "Event outside 24h range should not appear",
        )


# ---------------------------------------------------------------------------
# build_chart_payload
# ---------------------------------------------------------------------------


class BuildChartPayloadTests(TestCase):
    """Tests for Chart.js line chart config generation.

    The payload must be a valid Chart.js configuration object. If the
    structure is wrong, Chart.js silently renders nothing — so we verify
    the exact shape the frontend expects.
    """

    def test_basic_structure(self):
        """The payload should have type, data.labels, data.datasets, and options."""
        series = [
            (timezone.now() - timedelta(hours=2), 5),
            (timezone.now() - timedelta(hours=1), 10),
        ]
        config = build_chart_payload(
            series, label="Events", color="#6f42c1", bucket="hour"
        )

        self.assertEqual(config["type"], "line")
        self.assertEqual(len(config["data"]["labels"]), 2)
        self.assertEqual(len(config["data"]["datasets"]), 1)
        self.assertEqual(config["data"]["datasets"][0]["label"], "Events")
        self.assertEqual(config["data"]["datasets"][0]["data"], [5, 10])
        self.assertEqual(config["data"]["datasets"][0]["borderColor"], "#6f42c1")

    def test_hour_label_format(self):
        """Hourly buckets should include the time in labels (e.g., 'Mar 15 14:00')."""
        dt = timezone.now().replace(hour=14, minute=0, second=0, microsecond=0)
        series = [(dt, 1)]
        config = build_chart_payload(series, label="X", color="#000", bucket="hour")
        label = config["data"]["labels"][0]
        self.assertIn("14:00", label)

    def test_day_label_format(self):
        """Daily buckets should show date only, no time (e.g., 'Mar 15')."""
        dt = timezone.now().replace(hour=14, minute=0, second=0, microsecond=0)
        series = [(dt, 1)]
        config = build_chart_payload(series, label="X", color="#000", bucket="day")
        label = config["data"]["labels"][0]
        self.assertNotIn(":", label, "Day labels should not include time")

    def test_empty_series(self):
        """An empty series should produce a valid config with empty arrays."""
        config = build_chart_payload([], label="X", color="#000", bucket="hour")
        self.assertEqual(config["data"]["labels"], [])
        self.assertEqual(config["data"]["datasets"][0]["data"], [])

    def test_y_axis_begins_at_zero(self):
        """The y-axis should always start at zero to avoid misleading charts."""
        config = build_chart_payload([], label="X", color="#000", bucket="hour")
        self.assertTrue(config["options"]["scales"]["y"]["beginAtZero"])

    def test_integer_precision_on_y_axis(self):
        """The y-axis ticks should use integer precision (no decimals for counts)."""
        config = build_chart_payload([], label="X", color="#000", bucket="hour")
        self.assertEqual(config["options"]["scales"]["y"]["ticks"]["precision"], 0)


# ---------------------------------------------------------------------------
# build_stacked_bar_payload
# ---------------------------------------------------------------------------


class BuildStackedBarPayloadTests(TestCase):
    """Tests for Chart.js stacked bar chart config generation.

    The stacked bar chart is used by the Users widget to show API vs Web
    users. Each series must use the same x-axis labels, and the datasets
    must be stacked.
    """

    def test_empty_series_dict(self):
        """An empty series dict should return a valid but empty chart config."""
        config = build_stacked_bar_payload({}, colors={}, bucket="day")
        self.assertEqual(config["type"], "bar")
        self.assertEqual(config["data"]["labels"], [])
        self.assertEqual(config["data"]["datasets"], [])

    def test_multiple_series_share_labels(self):
        """All series should share the same x-axis labels from the first series."""
        now = timezone.now()
        series_a = [(now - timedelta(hours=2), 1), (now - timedelta(hours=1), 2)]
        series_b = [(now - timedelta(hours=2), 3), (now - timedelta(hours=1), 4)]

        config = build_stacked_bar_payload(
            {"A": series_a, "B": series_b},
            colors={"A": "#f00", "B": "#0f0"},
            bucket="hour",
        )
        self.assertEqual(len(config["data"]["labels"]), 2)
        self.assertEqual(len(config["data"]["datasets"]), 2)
        self.assertEqual(config["data"]["datasets"][0]["data"], [1, 2])
        self.assertEqual(config["data"]["datasets"][1]["data"], [3, 4])

    def test_stacked_axis_config(self):
        """Both x and y axes should have stacked=True for proper stacking."""
        config = build_stacked_bar_payload({}, colors={}, bucket="day")
        self.assertTrue(config["options"]["scales"]["x"]["stacked"])
        self.assertTrue(config["options"]["scales"]["y"]["stacked"])

    def test_colors_applied(self):
        """Each dataset should use the color from the colors dict."""
        now = timezone.now()
        series = {"API": [(now, 1)], "Web": [(now, 2)]}
        config = build_stacked_bar_payload(
            series,
            colors={"API": "#0d6efd", "Web": "#20c997"},
            bucket="hour",
        )
        self.assertEqual(config["data"]["datasets"][0]["backgroundColor"], "#0d6efd")
        self.assertEqual(config["data"]["datasets"][1]["backgroundColor"], "#20c997")

    def test_fallback_color_for_missing_key(self):
        """A series not in the colors dict should get the fallback color."""
        now = timezone.now()
        config = build_stacked_bar_payload(
            {"Unknown": [(now, 1)]},
            colors={},
            bucket="day",
        )
        self.assertEqual(
            config["data"]["datasets"][0]["backgroundColor"],
            "#20c997",
            "Missing color key should use fallback teal",
        )


# ---------------------------------------------------------------------------
# Time range edge cases
# ---------------------------------------------------------------------------


class TimeRangeEdgeCaseTests(TestCase):
    """Tests for time range resolution and bucket selection edge cases.

    These ensure the dashboard behaves correctly at boundary conditions:
    invalid slugs, very short ranges, and the bucket granularity switch
    point (3 days).
    """

    def test_invalid_slug_defaults_to_24h(self):
        """An unrecognized time range slug should fall back to 24h."""
        time_range = resolve_time_range("invalid-slug")
        self.assertEqual(time_range.slug, "24h")

    def test_none_slug_defaults_to_24h(self):
        """A None slug (no query param) should fall back to 24h."""
        time_range = resolve_time_range(None)
        self.assertEqual(time_range.slug, "24h")

    def test_1h_range_uses_hourly_buckets(self):
        """The shortest range (1h) should use hourly granularity."""
        time_range = resolve_time_range("1h")
        self.assertEqual(time_range.select_bucket_granularity(), "hour")

    def test_7d_range_uses_daily_buckets(self):
        """A 7-day range should use daily granularity (> 3 day threshold)."""
        time_range = resolve_time_range("7d")
        self.assertEqual(time_range.select_bucket_granularity(), "day")

    def test_24h_range_uses_hourly_buckets(self):
        """A 24-hour range (1 day, <= 3 days) should use hourly granularity."""
        time_range = resolve_time_range("24h")
        self.assertEqual(time_range.select_bucket_granularity(), "hour")

    def test_start_is_before_end(self):
        """The resolved start should always be before end."""
        for slug in ("1h", "6h", "24h", "7d", "14d", "30d", "90d"):
            time_range = resolve_time_range(slug)
            self.assertLess(
                time_range.start,
                time_range.end,
                f"start should be before end for {slug}",
            )


# ---------------------------------------------------------------------------
# Widget org scoping (view-level guard)
# ---------------------------------------------------------------------------


class WidgetOrgGuardTests(TestCase):
    """Tests for the org=None guard in WidgetDetailView.

    Workflow guests and users with broken session state can end up with
    no current org. Without a guard, widgets would query without an org
    filter and leak data across organizations.
    """

    def test_widget_returns_404_when_no_org(self):
        """A user whose get_current_org() returns None should get a 404.

        We mock get_current_org to return None to simulate a workflow
        guest or a user whose org was deleted. In practice,
        _has_dashboard_access would also block these users, but this
        guard is defense-in-depth.
        """
        from unittest.mock import patch

        from django.urls import reverse

        from validibot.users.constants import RoleCode

        user = UserFactory()
        org = user.orgs.first()
        user.set_current_org(org)
        membership = user.memberships.first()
        membership.add_role(RoleCode.ADMIN)
        self.client.force_login(user)
        session = self.client.session
        session["active_org_id"] = org.id
        session.save()

        # Simulate a broken state where get_current_org returns None
        # after the access check has already passed.
        with patch.object(type(user), "get_current_org", return_value=None):
            response = self.client.get(
                reverse(
                    "dashboard:widget-detail", kwargs={"slug": "total-validations"}
                ),
                {"time_range": "24h"},
            )
        self.assertEqual(response.status_code, 404)

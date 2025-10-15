from __future__ import annotations

import json
from typing import Any

from simplevalidations.dashboard.services import build_chart_payload
from simplevalidations.dashboard.services import generate_time_series
from simplevalidations.dashboard.widgets.base import DashboardWidget
from simplevalidations.dashboard.widgets.base import register_widget
from simplevalidations.tracking.models import TrackingEvent


@register_widget
class EventsTimeSeriesWidget(DashboardWidget):
    slug = "events-time-series"
    title = "Events"
    description = "Event volume aggregated by the selected time frame."
    template_name = "dashboard/widgets/events_time_series.html"
    width = "col-xl-6 col-lg-12"

    def get_context_data(self) -> dict[str, Any]:
        org = self.get_org()
        qs = TrackingEvent.objects.all()
        if org:
            qs = qs.filter(org=org)
        bucket = self.time_range.select_bucket_granularity()
        series = generate_time_series(
            qs,
            time_range=self.time_range,
            bucket=bucket,
        )
        chart_config = build_chart_payload(
            series,
            label="Events",
            color="#6f42c1",
            bucket=bucket,
        )
        values = [value for _, value in series]
        return {
            "chart_config": chart_config,
            "chart_config_json": json.dumps(chart_config),
            "has_data": any(values),
        }


@register_widget
class UsersTimeSeriesWidget(DashboardWidget):
    slug = "users-time-series"
    title = "Users"
    description = "Unique users interacting with the platform per interval."
    template_name = "dashboard/widgets/users_time_series.html"
    width = "col-xl-6 col-lg-12"

    def get_context_data(self) -> dict[str, Any]:
        org = self.get_org()
        qs = TrackingEvent.objects.filter(user__isnull=False)
        if org:
            qs = qs.filter(org=org)
        bucket = self.time_range.select_bucket_granularity()
        series = generate_time_series(
            qs,
            time_range=self.time_range,
            bucket=bucket,
            value_field="user_id",
            distinct=True,
        )
        chart_config = build_chart_payload(
            series,
            label="Users",
            color="#20c997",
            bucket=bucket,
        )
        values = [value for _, value in series]
        return {
            "chart_config": chart_config,
            "chart_config_json": json.dumps(chart_config),
            "has_data": any(values),
        }

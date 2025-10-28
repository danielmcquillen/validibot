from __future__ import annotations

from collections import OrderedDict
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db.models import Count
from django.db.models import QuerySet
from django.db.models.functions import TruncDay
from django.db.models.functions import TruncHour
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from simplevalidations.dashboard.time_ranges import ResolvedTimeRange


def _truncate_qs(qs: QuerySet, *, bucket: str):
    trunc_field = TruncHour("created") if bucket == "hour" else TruncDay("created")
    return qs.annotate(period=trunc_field)


def _align_to_bucket(dt: datetime, *, bucket: str) -> datetime:
    dt = timezone.localtime(dt)
    if bucket == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def generate_time_series(
    queryset: QuerySet,
    *,
    time_range: ResolvedTimeRange,
    bucket: str,
    value_field: str = "id",
    distinct: bool = False,
) -> list[tuple[datetime, int]]:
    """
    Aggregate ``queryset`` into a time-series within ``time_range``.

    Args:
        queryset: Base queryset scoped to org/time window.
        time_range: Resolved time range to use.
        bucket: ``"hour"`` or ``"day"``.
        value_field: Field passed to Count().
        distinct: Whether to count distinct values.

    Returns:
        List of (period, value) tuples, where period is timezone-aware datetime.
        Periods with no data will have a value of 0.
    """
    qs = queryset.filter(
        created__gte=time_range.start,
        created__lt=time_range.end,
    )
    qs = _truncate_qs(qs, bucket=bucket)

    count_kwargs = {"distinct": True} if distinct else {}
    aggregated = (
        qs.values("period")
        .order_by("period")
        .annotate(total=Count(value_field, **count_kwargs))
    )

    period_to_value: OrderedDict[datetime, int] = OrderedDict()
    aligned_start = _align_to_bucket(time_range.start, bucket=bucket)
    aligned_end = _align_to_bucket(time_range.end, bucket=bucket)
    current = aligned_start

    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    # Ensure the end boundary is exclusive for iteration.
    while current < aligned_end:
        period_to_value[current] = 0
        current += step

    for row in aggregated:
        period = timezone.localtime(row["period"])
        key = _align_to_bucket(period, bucket=bucket)
        period_to_value[key] = row["total"]

    return list(period_to_value.items())


def build_chart_payload(
    series: Iterator[tuple[datetime, int]],
    *,
    label: str,
    color: str,
    bucket: str,
) -> dict:
    """
    Build chart.js configuration payload for a time-series.

    Args:
        series (Iterable[Tuple[datetime, int]]): Time-series data points.
        label (str): Label for the data series.
        color (str): CSS color for the line/points.
        bucket (str): Either ``"hour"`` or ``"day"``.

    Returns:
        dict: Chart.js configuration dictionary.
    """
    labels: list[str] = []
    values: list[int] = []
    for period, value in series:
        if bucket == "hour":
            formatted = period.strftime("%b %d %H:%M")
        else:
            formatted = period.strftime("%b %d")
        labels.append(formatted)
        values.append(int(value))

    chart_config = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": label,
                    "data": values,
                    "fill": False,
                    "borderColor": color,
                    "backgroundColor": color,
                    "tension": 0.3,
                },
            ],
        },
        "options": {
            "responsive": True,
            "maintainAspectRatio": False,
            "scales": {
                "x": {
                    "type": "category",
                    "ticks": {
                        "autoSkip": True,
                        "maxTicksLimit": 6,
                    },
                },
                "y": {
                    "beginAtZero": True,
                    "ticks": {
                        "precision": 0,
                    },
                },
            },
            "plugins": {
                "legend": {
                    "display": True,
                    "position": "bottom",
                },
            },
        },
    }

    return chart_config

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class TimeRangeOption:
    """
    Declarative description of a dashboard time range option.
    """

    slug: str
    label: str
    delta: timedelta


@dataclass(frozen=True)
class ResolvedTimeRange:
    """
    Concrete time window derived from a :class:`TimeRangeOption`.
    """

    option: TimeRangeOption
    start: datetime
    end: datetime

    @property
    def slug(self) -> str:
        return self.option.slug

    @property
    def label(self) -> str:
        return self.option.label

    @property
    def delta(self) -> timedelta:
        return self.option.delta

    def select_bucket_granularity(self) -> str:
        """
        Determine aggregation granularity for charts.

        Returns:
            str: Either ``"hour"`` or ``"day"``.
        """
        if self.delta <= timedelta(days=3):
            return "hour"
        return "day"


_TIME_RANGE_OPTIONS: tuple[TimeRangeOption, ...] = (
    TimeRangeOption("1h", "Last hour", timedelta(hours=1)),
    TimeRangeOption("6h", "Last 6 hours", timedelta(hours=6)),
    TimeRangeOption("24h", "Last 24 hours", timedelta(hours=24)),
    TimeRangeOption("7d", "Last 7 days", timedelta(days=7)),
    TimeRangeOption("14d", "Last 14 days", timedelta(days=14)),
    TimeRangeOption("30d", "Last 30 days", timedelta(days=30)),
    TimeRangeOption("90d", "Last 90 days", timedelta(days=90)),
)

_DEFAULT_SLUG = "24h"


def iter_time_range_options() -> Iterator[TimeRangeOption]:
    return _TIME_RANGE_OPTIONS


def get_time_range_option(slug: str | None) -> TimeRangeOption:
    if slug:
        for option in _TIME_RANGE_OPTIONS:
            if option.slug == slug:
                return option
    for option in _TIME_RANGE_OPTIONS:
        if option.slug == _DEFAULT_SLUG:
            return option
    return _TIME_RANGE_OPTIONS[0]


def resolve_time_range(
    slug: str | None,
    *,
    reference: datetime | None = None,
) -> ResolvedTimeRange:
    option = get_time_range_option(slug)
    reference = reference or timezone.now()
    end = reference
    start = end - option.delta
    return ResolvedTimeRange(option=option, start=start, end=end)

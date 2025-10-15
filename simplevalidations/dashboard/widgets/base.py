"""
Base classes and registry for dashboard widgets. Dashboard widgets are
pluggable components that can be displayed on a dashboard page. They usually
show summary statistics, charts, or other visualizations based on data.

A dashboard widget is a class that subclasses DashboardWidget and implements
the get_context_data() method to provide context for rendering its template.
You won't find any concrete implementations here; those are in other modules.

"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Iterator

    from django.http import HttpRequest

    from simplevalidations.dashboard.time_ranges import ResolvedTimeRange


class WidgetRegistrationError(RuntimeError):
    """Raised when a widget fails to register with the dashboard registry."""

    pass


class DashboardWidget:
    """
    Base class for dashboard widgets.

    Subclasses must define ``slug``, ``title`` and ``template_name``.
    """

    slug: str = ""
    title: str = ""
    description: str = ""
    template_name: str = ""
    width: str = "col-xl-3 col-md-6"

    def __init__(self, *, request: HttpRequest, time_range: ResolvedTimeRange):
        self.request = request
        self.time_range = time_range
        self._org = None

    def get_context_data(self) -> dict[str, Any]:
        """Return template context for this widget."""
        return {}

    def as_context(self) -> dict[str, Any]:
        context = {"widget": self}
        context.update(self.get_context_data())
        return context

    def get_org(self):
        """
        Resolve the organization scoped to the current user.
        """
        if self._org is not None:
            return self._org
        user = getattr(self.request, "user", None)
        if user and hasattr(user, "get_current_org"):
            self._org = user.get_current_org()
        return self._org


@dataclass(frozen=True)
class WidgetDefinition:
    slug: str
    title: str
    description: str
    width: str
    template_name: str
    widget_class: type[DashboardWidget]

    def instantiate(
        self,
        *,
        request: HttpRequest,
        time_range: ResolvedTimeRange,
    ) -> DashboardWidget:
        return self.widget_class(request=request, time_range=time_range)


class DashboardWidgetRegistry:
    """
    Simple registry of dashboard widgets keyed by slug.
    """

    def __init__(self) -> None:
        self._widget_registry: dict[str, WidgetDefinition] = {}

    def register(self, widget_cls: type[DashboardWidget]) -> type[DashboardWidget]:
        if not widget_cls.slug:
            raise WidgetRegistrationError(f"{widget_cls.__name__} must define slug.")
        if widget_cls.slug in self._widget_registry:
            raise WidgetRegistrationError(
                f"Widget slug '{widget_cls.slug}' already registered."
            )
        if not widget_cls.title:
            raise WidgetRegistrationError(f"{widget_cls.__name__} must define title.")
        if not widget_cls.template_name:
            raise WidgetRegistrationError(
                f"{widget_cls.__name__} must define template_name.",
            )

        definition = WidgetDefinition(
            slug=widget_cls.slug,
            title=widget_cls.title,
            description=widget_cls.description,
            width=widget_cls.width,
            template_name=widget_cls.template_name,
            widget_class=widget_cls,
        )
        self._widget_registry[widget_cls.slug] = definition
        return widget_cls

    def get(self, slug: str) -> WidgetDefinition:
        try:
            return self._widget_registry[slug]
        except KeyError as exc:
            msg = f"No dashboard widget registered under slug '{slug}'"
            raise WidgetRegistrationError(msg) from exc

    def __iter__(self) -> Iterator[WidgetDefinition]:
        return iter(self._widget_registry.values())

    def items(self) -> Iterable[tuple[str, WidgetDefinition]]:
        return self._widget_registry.items()


registry = DashboardWidgetRegistry()


def register_widget(widget_cls: type[DashboardWidget]) -> type[DashboardWidget]:
    return registry.register(widget_cls)

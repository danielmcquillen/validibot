from .base import DashboardWidget, WidgetDefinition, registry  # noqa: F401

# Import built-in widgets so they register with the registry on app load.
from . import metrics  # noqa: F401
from . import timeseries  # noqa: F401

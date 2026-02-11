# Import built-in widgets so they register with the registry on app load.
from . import metrics  # noqa: F401
from . import timeseries  # noqa: F401
from .base import DashboardWidget  # noqa: F401
from .base import WidgetDefinition  # noqa: F401
from .base import registry  # noqa: F401

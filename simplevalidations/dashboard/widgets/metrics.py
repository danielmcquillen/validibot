from __future__ import annotations

from typing import Any, Dict

from simplevalidations.dashboard.widgets.base import DashboardWidget, register_widget
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.models import ValidationFinding, ValidationRun


@register_widget
class TotalValidationsWidget(DashboardWidget):
    slug = "total-validations"
    title = "Number of Validations"
    description = "Total validation runs executed in the selected window."
    template_name = "dashboard/widgets/total_validations.html"
    width = "col-xl-3 col-md-6"

    def get_context_data(self) -> Dict[str, Any]:
        org = self.get_org()
        qs = ValidationRun.objects.all()
        if org:
            qs = qs.filter(org=org)
        qs = qs.filter(
            created__gte=self.time_range.start,
            created__lt=self.time_range.end,
        )
        return {"total_count": qs.count()}


@register_widget
class TotalErrorsWidget(DashboardWidget):
    slug = "total-errors"
    title = "Number of Errors"
    description = "Sum of validation findings reported as errors."
    template_name = "dashboard/widgets/total_errors.html"
    width = "col-xl-3 col-md-6"

    def get_context_data(self) -> Dict[str, Any]:
        org = self.get_org()
        qs = ValidationFinding.objects.filter(
            severity=Severity.ERROR,
            created__gte=self.time_range.start,
            created__lt=self.time_range.end,
        )
        if org:
            qs = qs.filter(validation_run__org=org)
        return {"total_count": qs.count()}

from __future__ import annotations

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout
from django import forms
from django.utils.translation import gettext_lazy as _

from validibot.projects.models import LUMINANCE_THRESHOLD
from validibot.projects.models import Project
from validibot.projects.models import generate_random_color

HEX_VALUES_LENGTH = 7  # e.g., #RRGGBB


def _normalize_hex(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if not trimmed.startswith("#"):
        trimmed = f"#{trimmed}"
    if len(trimmed) != HEX_VALUES_LENGTH:
        return None
    try:
        int(trimmed[1:], 16)
    except ValueError:
        return None
    return trimmed.upper()


def _lighten_hex(value: str, factor: float = 0.35) -> str:
    normalized = _normalize_hex(value) or Project.DEFAULT_BADGE_COLOR
    r = int(normalized[1:3], 16)
    g = int(normalized[3:5], 16)
    b = int(normalized[5:7], 16)

    def lighten(channel: int) -> int:
        return min(255, int(channel + (255 - channel) * factor))

    return f"#{lighten(r):02X}{lighten(g):02X}{lighten(b):02X}"


def _contrast_hex(value: str) -> str:
    normalized = _normalize_hex(value) or Project.DEFAULT_BADGE_COLOR
    r = int(normalized[1:3], 16)
    g = int(normalized[3:5], 16)
    b = int(normalized[5:7], 16)
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#1F2328" if luminance > LUMINANCE_THRESHOLD else "#FFFFFF"


class ProjectColorWidget(forms.TextInput):
    template_name = "projects/forms/widgets/project_color_widget.html"

    def __init__(self, attrs: dict[str, str] | None = None):
        base_attrs = {
            "class": "form-control project-color-input",
            "maxlength": "7",
            "placeholder": "#0366D6",
            "autocomplete": "off",
            "spellcheck": "false",
            "data-testid": "project-color-input",
        }
        if attrs:
            base_attrs.update(attrs)
        super().__init__(base_attrs)

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        widget = context["widget"]
        current_color = (
            _normalize_hex(widget.get("value"))
            or _normalize_hex(widget["attrs"].get("value"))
            or Project.DEFAULT_BADGE_COLOR
        )
        widget["attrs"]["data-color-input"] = widget["attrs"].get("id")
        widget["attrs"]["data-initial-color"] = current_color
        context.update(
            {
                "initial_color": current_color,
                "border_color": _lighten_hex(current_color),
                "icon_color": _contrast_hex(current_color),
            },
        )
        return context


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "description", "color"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "color": ProjectColorWidget(),
        }
        help_texts = {
            "color": _("Used for workflow badges and other UI accents."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_tag = False
        self.helper.layout = Layout("name", "description", "color")

        if not self.is_bound and not self.instance.pk and not self.initial.get("color"):
            color = getattr(self.instance, "color", None) or generate_random_color()
            self.initial["color"] = color
            self.instance.color = color

        color_field = self.fields.get("color")
        if color_field:
            color_field.required = True
            color_field.label = _("Badge color")

    def clean_color(self):
        color = _normalize_hex(self.cleaned_data.get("color"))
        if not color:
            raise forms.ValidationError(Project.HEX_COLOR_MESSAGE)
        return color

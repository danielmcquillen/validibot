from __future__ import annotations

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from simplevalidations.actions.constants import CertificationActionType
from simplevalidations.actions.constants import IntegrationActionType
from simplevalidations.actions.models import Action
from simplevalidations.actions.models import ActionDefinition
from simplevalidations.actions.models import SignedCertificateAction
from simplevalidations.actions.models import SlackMessageAction
from simplevalidations.actions.registry import register_action_form
from simplevalidations.workflows.forms import BaseStepConfigForm


class BaseWorkflowActionForm(BaseStepConfigForm):
    """Base form for configuring workflow actions with custom fields."""

    action_model: type[Action] = Action

    def __init__(self, *args, definition: ActionDefinition, step=None, **kwargs):
        self.definition = definition
        self._step = step
        self._variant = None
        self._existing_action = getattr(step, "action", None) if step else None
        super().__init__(*args, step=step, **kwargs)
        self.fields.pop("display_schema", None)

        if self._existing_action:
            action = self._existing_action
            self.fields["name"].initial = action.name
            self.fields["description"].initial = action.description
            variant = self._get_variant(action)
            if variant is not None:
                self._variant = variant
                self.populate_variant_initial(variant)
        else:
            self.fields["name"].initial = definition.name
            self.fields["description"].initial = definition.description

    # Hook methods ----------------------------------------------------------

    def populate_variant_initial(self, action: Action) -> None:
        """Load existing action values into form fields."""

    def update_variant(self, action: Action) -> None:
        """Update the action subtype with cleaned data."""

    def build_step_summary(self, action: Action) -> dict[str, Any]:
        """Optional summary persisted on WorkflowStep.config."""
        return {}

    # Helpers ----------------------------------------------------------------

    def _get_variant(self, action: Action) -> Action | None:
        model_cls = self.action_model
        if model_cls is Action:
            return action
        if isinstance(action, model_cls):
            return action
        try:
            return model_cls.objects.get(pk=action.pk)
        except model_cls.DoesNotExist:
            return None

    # Persistence ------------------------------------------------------------

    def save_action(
        self,
        definition: ActionDefinition,
        *,
        current_action: Action | None = None,
    ) -> Action:
        """
        Create or update the concrete action model for this form.
        """

        target: Action | None = None
        if current_action:
            target = self._get_variant(current_action)

        if target is None:
            target = self.action_model(definition=definition)
        target.definition = definition
        target.name = (self.cleaned_data.get("name") or "").strip() or definition.name
        target.description = (self.cleaned_data.get("description") or "").strip()
        self.update_variant(target)
        target.save()
        return target


class SlackMessageActionForm(BaseWorkflowActionForm):
    """Collect the Slack message text for integration steps.

    Example:
        SlackMessageActionForm(data={"name": "Notify", "message": "Workflow finished"})
    """

    action_model = SlackMessageAction

    message = forms.CharField(
        label=_("Message"),
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=_("Message content delivered to Slack."),
    )

    def populate_variant_initial(self, action: SlackMessageAction) -> None:
        self.fields["message"].initial = action.message

    def update_variant(self, action: SlackMessageAction) -> None:
        action.message = self.cleaned_data.get("message", "").strip()

    def build_step_summary(self, action: SlackMessageAction) -> dict[str, Any]:
        return {"message": action.message}


class SignedCertificateActionForm(BaseWorkflowActionForm):
    """Collect certificate template uploads for certification steps."""

    action_model = SignedCertificateAction

    certificate_template = forms.FileField(
        label=_("Certificate template"),
        required=False,
        help_text=_(
            "Upload a PDF template. Leave empty to use the default template.",
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self._variant and getattr(self._variant, "certificate_template", None):
            current_name = self._variant.certificate_template.name
            if current_name:
                self.fields["certificate_template"].help_text = _(
                    "Upload a new template to replace '%(name)s'.",
                ) % {"name": current_name.split("/")[-1]}

    def update_variant(self, action: SignedCertificateAction) -> None:
        template = self.cleaned_data.get("certificate_template")
        if template:
            action.certificate_template = template

    def build_step_summary(self, action: SignedCertificateAction) -> dict[str, Any]:
        return {
            "certificate_template": action.get_certificate_template_display_name(),
        }


register_action_form(IntegrationActionType.SLACK_MESSAGE, SlackMessageActionForm)
register_action_form(
    CertificationActionType.SIGNED_CERTIFICATE, SignedCertificateActionForm
)

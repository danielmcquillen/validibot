from __future__ import annotations

import uuid

from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import ActionFailureMode
from validibot.actions.constants import IntegrationActionType
from validibot.actions.registry import get_action_model
from validibot.actions.registry import register_action_model


class ActionDefinition(TimeStampedModel):
    """Catalog entry for reusable workflow actions.

    Each definition describes a non-validation step the workflow builder can
    attach to a workflow: integrations (e.g. Slack notifications) and
    certifications (e.g. issuing a credential). Runtime execution delegates to
    handlers keyed by ``action_category`` and ``type``.
    """

    slug = models.SlugField(unique=True)

    name = models.CharField(max_length=200)

    description = models.TextField(blank=True, default="")

    icon = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text=_(
            "Bootstrap icon class to render when displaying "
            "the action (e.g. 'bi-slack').",
        ),
    )

    action_category = models.CharField(
        max_length=32,
        choices=ActionCategoryType.choices,
    )

    type = models.CharField(
        max_length=64,
        help_text=_("Implementation type identifier (e.g. SLACK_MESSAGE)."),
    )

    config_schema = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Optional JSON schema describing the configuration for this action.",
        ),
    )
    is_active = models.BooleanField(default=True)

    required_commercial_feature = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_(
            "Commercial feature flag required for this action to appear "
            "in the step picker (e.g. 'signed_credentials'). Leave blank for "
            "actions available to all installations."
        ),
    )

    class Meta:
        ordering = ["action_category", "name"]
        unique_together = [("action_category", "type")]

    def __str__(self) -> str:
        return f"{self.get_action_category_display()} · {self.name}"


class Action(TimeStampedModel):
    """Concrete action instance attached to a workflow step.

    An :class:`Action` copies metadata from its
    :class:`ActionDefinition` and stores any per-step configuration chosen by the
    workflow author.
    """

    id = models.BigAutoField(primary_key=True)
    definition = models.ForeignKey(
        ActionDefinition,
        on_delete=models.PROTECT,
        related_name="actions",
    )
    slug = models.SlugField(unique=True, blank=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    config = models.JSONField(default=dict, blank=True)

    failure_mode = models.CharField(
        max_length=16,
        choices=ActionFailureMode.choices,
        default=ActionFailureMode.BLOCKING,
        help_text=_(
            "How this action's failure affects the run. "
            "BLOCKING: run fails. ADVISORY: step is marked failed "
            "but the run may still succeed."
        ),
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        if not self.name or not self.name.strip():
            raise models.ValidationError({"name": _("Name is required.")})

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name) or uuid.uuid4().hex[:12]
            candidate = base_slug
            counter = 2
            queryset = self.__class__.objects
            while queryset.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base_slug}-{counter}"
                counter += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    def get_variant(self):
        """
        Return the concrete subclass instance for this action, if any.
        """

        if not self.definition_id:
            return self
        model_cls = get_action_model(self.definition.type)
        if model_cls is Action:
            return self
        if isinstance(self, model_cls):
            return self
        try:
            return model_cls.objects.get(pk=self.pk)
        except model_cls.DoesNotExist:
            return None


class SlackMessageAction(Action):
    """Action that posts a notification to Slack.

    Example:
        SlackMessageAction(definition=definition, message="Workflow finished")
    """

    message = models.TextField()


register_action_model(
    IntegrationActionType.SLACK_MESSAGE,
    SlackMessageAction,
)

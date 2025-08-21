from django.db import models
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel
from psycopg import Time

from roscoe.users.models import Organization, User
from roscoe.validations.constants import RulesetType, ValidationType


class Ruleset(TimeStampedModel):
    """
    Schema or rule bundle (JSON Schema, XSD, YAML rules, etc.)
    Can be global (org=None) or org-private.
    """

    class Meta:
        indexes = [
            models.Index(
                fields=[
                    "org",
                    "type",
                ]
            )
        ]

    org = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="rulesets",
    )

    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rulesets",
        help_text=_("The user who created this ruleset."),
    )

    name = models.CharField(max_length=200)

    type = models.CharField(
        max_length=40,
        choices=RulesetType.choices,
        help_text=_("Type of validation ruleset, e.g. 'json_schema', 'xml_schema'"),
    )

    version = models.CharField(max_length=40, blank=True, default="")

    file = models.FileField(upload_to="rulesets/")  # or TextField for inline content

    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)


class Validator(TimeStampedModel):
    """
    A pluggable validator 'type' and version.
    Examples:
      kind='json_schema', version='2020-12'
      kind='xml_schema', version='1.0'
      kind='energyplus', version='23.1'
    """

    class Meta:
        unique_together = [
            (
                "slug",
                "version",
            )
        ]
        indexes = [
            models.Index(
                fields=[
                    "type",
                    "slug",
                ]
            )
        ]

    slug = models.SlugField(
        null=False,
        blank=True,
        help_text=_(
            "A unique identifier for the validator, used in URLs."
        ),  # e.g. "json-2020-12", "eplus-23-1"
    )

    name = models.CharField(max_length=120)  # display label

    type = models.CharField(
        max_length=40,
        choices=ValidationType.choices,
        null=False,
        blank=False,
    )
    version = models.PositiveIntegerField(
        help_text=_("Version of the validator, e.g. 1, 2, 3")
    )

    is_public = models.BooleanField(default=True)  # false for org-private validators

    default_ruleset = models.ForeignKey(
        Ruleset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

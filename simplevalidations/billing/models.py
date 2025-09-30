# Create your models here.
from django.db import models
from django_extensions.db.models import TimeStampedModel

from simplevalidations.users.models import Organization


class OrgQuota(TimeStampedModel):
    org = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="quota",
    )
    max_submissions_per_day = models.IntegerField(default=100)

    max_run_minutes_per_day = models.IntegerField(default=60)  # for E+ or heavy jobs

    artifact_retention_days = models.IntegerField(default=7)


class UsageCounter(TimeStampedModel):
    """
    Roll up lightweight usage for fast checks; you can also compute from RequestLog.
    """

    class Meta:
        unique_together = [("org", "date")]
        indexes = [models.Index(fields=["org", "date"])]

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="usage_counters",
    )

    date = models.DateField()

    submissions = models.IntegerField(default=0)

    run_minutes = models.IntegerField(default=0)

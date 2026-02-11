from enum import Enum

from django.db import models
from django.utils.translation import gettext_lazy as _


class DeploymentTarget(str, Enum):
    """
    Deployment target for Validibot.

    This enum identifies the deployment environment, which controls:
    - Task dispatcher selection (how validation tasks are enqueued)
    - Execution backend selection (how validator containers are run)
    - Storage backend selection (where files are stored)

    Values:
        TEST: Test environment - synchronous inline execution, in-memory storage.
            Used automatically when running pytest.

        LOCAL_DOCKER_COMPOSE: Local development using Docker Compose.
            HTTP calls to worker container, local Docker for validators.

        DOCKER_COMPOSE: Production Docker Compose with Celery/Redis.
            Redis-backed task queue, local Docker for validators.

        GCP: Google Cloud Platform - Cloud Run with Cloud Tasks.
            Cloud Tasks queue, Cloud Run Jobs for validators, GCS storage.

        AWS: Amazon Web Services - ECS with SQS (future).
            SQS queue, AWS Batch/ECS for validators, S3 storage.
    """

    TEST = "test"
    LOCAL_DOCKER_COMPOSE = "local_docker_compose"
    DOCKER_COMPOSE = "docker_compose"
    GCP = "gcp"
    AWS = "aws"

    def __str__(self) -> str:
        return self.value


class RequestType(models.TextChoices):
    API = "API", _("API")
    UI = "UI", _("UI")
    GITHUB_APP = "GITHUB_APP", _("GitHub App")


class InviteStatus(models.TextChoices):
    """
    Shared status choices for all invite types.

    Used by MemberInvite, WorkflowInvite, and GuestInvite to standardize
    invite lifecycle states.
    """

    PENDING = "PENDING", _("Pending")
    ACCEPTED = "ACCEPTED", _("Accepted")
    DECLINED = "DECLINED", _("Declined")
    CANCELED = "CANCELED", _("Canceled")
    EXPIRED = "EXPIRED", _("Expired")

from enum import StrEnum

from django.db import models
from django.utils.translation import gettext_lazy as _


class DeploymentTarget(StrEnum):
    """
    Deployment target for Validibot.

    This enum identifies the deployment environment, which controls:
    - Task dispatcher selection (how validation tasks are enqueued)
    - Execution backend selection (how validator containers are run)
    - Storage backend selection (where files are stored)

    See ADR-2026-04-27 (Boring Self-Hosting and Operator Experience)
    for the audience-named taxonomy these values implement.

    Values:
        TEST: Test environment - synchronous inline execution, in-memory storage.
            Used automatically when running pytest.

        LOCAL_DOCKER_COMPOSE: Local development using Docker Compose.
            Celery via Redis for task dispatch, local Docker for validators.
            Audience: a single Validibot developer testing on their laptop.

        SELF_HOSTED: Customer-operated single-VM deployment using
            Docker Compose (typically on DigitalOcean, AWS EC2, Hetzner,
            or on-prem). Redis-backed task queue, local Docker for
            validators.
            Audience: customers running their own copy.

        GCP: Google Cloud Platform - Cloud Run with Cloud Tasks.
            Cloud Tasks queue, Cloud Run Jobs for validators, GCS storage.
            Audience: Validibot's hosted cloud offering.

        AWS: Amazon Web Services - ECS with SQS (future).
            SQS queue, AWS Batch/ECS for validators, S3 storage.

    Note: ``LOCAL_DOCKER_COMPOSE`` keeps its technology-named value
    because "local" describes the developer-dev audience. Production
    Docker Compose was renamed to ``SELF_HOSTED`` to reflect its
    customer-facing audience. The two share a substrate but differ in
    audience; that asymmetry is intentional.
    """

    TEST = "test"
    LOCAL_DOCKER_COMPOSE = "local_docker_compose"
    SELF_HOSTED = "self_hosted"
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

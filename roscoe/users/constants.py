from django.db import models


class MemberRole(models.TextChoices):
    """
    Enum for user roles within an organization.
    """

    OWNER = "owner", "Owner"
    MEMBER = "member", "Member"

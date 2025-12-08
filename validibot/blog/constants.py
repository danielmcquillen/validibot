"""Blog-level constants shared across views, models, and templates."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class BlogPostStatus(models.TextChoices):
    """Lifecycle states for marketing blog posts.

    Draft posts are visible only to staff members, while published posts surface
    through public listings, template helpers, and the sitemap we share with
    search engines.
    """

    DRAFT = "draft", _("Draft")
    PUBLISHED = "published", _("Published")

# Create your models here.
from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.files.storage import storages
from django.db import models
from django.db.models import TextField
from django.urls import reverse
from django.utils.html import strip_tags
from model_utils.models import TimeStampedModel

from validibot.blog.constants import BlogPostStatus
from validibot.core.mixins import FeaturedImageMixin

User = get_user_model()

CONTENT_PREVIEW_MAX_LENGTH = 200


def select_public_storage():
    """Return the public storage backend from STORAGES['public']."""
    return storages["public"]


class BlogPost(FeaturedImageMixin, TimeStampedModel):
    """Editorial content used on the marketing blog and sitemap.

    Blog posts are drafted by staff in the Django admin, optionally linked to an
    author, and exposed across marketing views once the status flips to
    ``BlogPostStatus.PUBLISHED``.
    """

    title = models.CharField(max_length=250, unique=False)

    summary = models.CharField(max_length=500, blank=True, default="")

    slug = models.SlugField(max_length=250, unique=True)

    featured_image = models.FileField(
        null=True,
        blank=True,
        # Use public media bucket - references STORAGES["public"] from settings
        storage=select_public_storage,
    )

    author = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="blog_posts",
    )

    content = TextField()

    published_on = models.DateTimeField(default=datetime.now, blank=True)

    status = models.CharField(
        max_length=16,
        choices=BlogPostStatus.choices,
        default=BlogPostStatus.DRAFT,
    )

    featured_image_credit = models.CharField(
        max_length=200,
        blank=True,
        default="",
    )

    featured_image_alt = models.CharField(
        max_length=500,
        blank=True,
        default="",
    )

    class Meta:
        ordering = ["-published_on"]

    def __str__(self):
        return self.title

    def get_content_preview(self) -> str:
        content = (self.content or "").strip()
        if not content:
            return ""
        preview = strip_tags(content).strip()
        return (
            (preview[:CONTENT_PREVIEW_MAX_LENGTH] + "...")
            if len(preview) > CONTENT_PREVIEW_MAX_LENGTH
            else preview
        )

    featured_image_alt_candidates = (
        "featured_image_alt",
        "title",
    )

    def get_absolute_url(self) -> str:
        return reverse(
            "marketing:blog:blog_post_detail",
            kwargs={"slug": self.slug},
        )

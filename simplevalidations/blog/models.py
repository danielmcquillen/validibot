# Create your models here.
from datetime import datetime

from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import TextField
from django.urls import reverse
from django.utils.html import strip_tags
from model_utils.models import TimeStampedModel

User = get_user_model()

STATUS = ((0, "Draft"), (1, "Publish"))


class BlogPost(TimeStampedModel):
    title = models.CharField(max_length=250, unique=False)

    summary = models.CharField(max_length=500, blank=True, default="")

    slug = models.SlugField(max_length=250, unique=True)

    featured_image = models.FileField(null=True, blank=True)

    author = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="blog_posts",
    )

    content = TextField()

    published_on = models.DateTimeField(default=datetime.now, blank=True)

    status = models.IntegerField(choices=STATUS, default=0)

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
        return (preview[:200] + "...") if len(preview) > 200 else preview

    def get_featured_image_alt(self) -> str:
        return (self.featured_image_alt or self.title or "").strip()

    def get_featured_image_url(self) -> str | None:
        image_file = getattr(self, "featured_image", None)
        if not image_file:
            return None
        try:
            return image_file.url
        except (ValueError, AttributeError):
            return None

    def has_featured_image(self) -> bool:
        exists = bool(self.get_featured_image_url())
        return exists

    def get_absolute_url(self) -> str:
        return reverse(
            "marketing:blog:blog_post_detail",
            kwargs={"slug": self.slug},
        )

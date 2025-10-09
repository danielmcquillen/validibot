# Create your models here.
from datetime import datetime

from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import TextField
from model_utils.models import TimeStampedModel

User = get_user_model()

STATUS = ((0, "Draft"), (1, "Publish"))


class BlogPost(TimeStampedModel):
    title = models.CharField(max_length=250, unique=False)

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

from django import template

from validibot.blog.constants import BlogPostStatus
from validibot.blog.models import BlogPost

register = template.Library()


@register.simple_tag
def most_recent_blog_post():
    """Return the most recently published blog post or ``None``."""

    if BlogPost.objects.count() == 0:
        return None
    return (
        BlogPost.objects.filter(status=BlogPostStatus.PUBLISHED)
        .order_by("-published_on")
        .select_related("author")
        .first()
    )

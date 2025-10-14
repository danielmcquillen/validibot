from django import template

from simplevalidations.blog.models import BlogPost

register = template.Library()


@register.simple_tag
def most_recent_blog_post():
    """Return the most recently published blog post or ``None``."""

    return (
        BlogPost.objects.filter(status=1)  # published posts
        .order_by("-published_on")
        .select_related("author")
        .first()
    )

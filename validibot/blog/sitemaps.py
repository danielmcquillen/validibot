from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from validibot.blog.constants import BlogPostStatus
from validibot.blog.models import BlogPost


class BlogPostSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.55
    limit = 200  # keep the paginator cheap even if you publish a lot

    def items(self):
        return (
            BlogPost.objects.filter(status=BlogPostStatus.PUBLISHED)
            .only("slug", "published_on", "modified")
            .order_by("-published_on")
        )

    def lastmod(self, obj: BlogPost):
        return getattr(obj, "modified", None) or obj.published_on

    def location(self, obj):
        return reverse("marketing:blog:blog_post_detail", kwargs={"slug": obj.slug})

from django.contrib.sitemaps import Sitemap

from simplevalidations.blog.models import BlogPost


class BlogPostSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.55

    def items(self):
        return (
            BlogPost.objects.filter(status=1)
            .order_by("-published_on")
            .only("slug", "published_on", "modified")
        )

    def lastmod(self, obj: BlogPost):
        return getattr(obj, "modified", None) or obj.published_on

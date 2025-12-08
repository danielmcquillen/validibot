from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from validibot.blog.constants import BlogPostStatus
from validibot.blog.models import BlogPost
from validibot.blog.sitemaps import BlogPostSitemap
from validibot.blog.templatetags.blog_tags import most_recent_blog_post


class BlogStatusBehaviorsTests(TestCase):
    """Verify that draft BlogPosts remain private while published posts surface."""

    def setUp(self):
        self.published = BlogPost.objects.create(
            title="Published",
            slug="published",
            content="ready",
            status=BlogPostStatus.PUBLISHED,
            published_on=timezone.now(),
        )
        self.draft = BlogPost.objects.create(
            title="Draft",
            slug="draft",
            content="not ready",
            status=BlogPostStatus.DRAFT,
            published_on=timezone.now(),
        )

    def test_list_view_excludes_drafts_for_public_requests(self):
        response = self.client.get(reverse("marketing:blog:blog_list"))
        self.assertEqual(response.status_code, 200)
        blog_posts = response.context["blog_posts"]
        self.assertEqual(list(blog_posts), [self.published])

    def test_sitemap_only_yields_published_posts(self):
        sitemap = BlogPostSitemap()
        items = list(sitemap.items())
        self.assertEqual(items, [self.published])

    def test_sitemap_location_uses_marketing_blog_namespace(self):
        sitemap = BlogPostSitemap()
        location = sitemap.location(self.published)
        expected_url = reverse(
            "marketing:blog:blog_post_detail",
            kwargs={"slug": self.published.slug},
        )
        self.assertEqual(location, expected_url)

    def test_template_tag_returns_latest_published_post(self):
        latest = most_recent_blog_post()
        self.assertEqual(latest, self.published)

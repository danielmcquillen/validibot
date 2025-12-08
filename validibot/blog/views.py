# Create your views here.
from typing import Any

from django.templatetags.static import static
from django.urls import reverse_lazy
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from django.views import generic

from validibot.blog.constants import BlogPostStatus
from validibot.blog.models import BlogPost
from validibot.core.mixins import BreadcrumbMixin
from validibot.marketing.constants import MarketingShareImage


class BlogPostList(BreadcrumbMixin, generic.ListView):
    template_name = "blog/blog_post_list.html"
    context_object_name = "blog_posts"
    breadcrumbs = [
        {
            "name": _("Resources"),
            "url": reverse_lazy("marketing:resources"),
        },
        {
            "name": _("Blog"),
            "url": reverse_lazy("marketing:blog:blog_list"),
        },
    ]

    def get_queryset(self):
        queryset = BlogPost.objects.select_related("author").order_by("-published_on")
        user = self.request.user
        if not user.is_staff and not user.is_superuser:
            queryset = queryset.filter(status=BlogPostStatus.PUBLISHED)
        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "section": "blog",
                "page_title": _("Blog"),
                "page_subtitle": _(
                    "Insights, guides, and product updates from the Validibot team.",
                ),
                "has_drafts": context["blog_posts"]
                .filter(status=BlogPostStatus.DRAFT)
                .exists()
                if self.request.user.is_staff or self.request.user.is_superuser
                else False,
            },
        )
        return context


class BlogPostDetail(BreadcrumbMixin, generic.DetailView):
    model = BlogPost
    template_name = "blog/blog_post_detail.html"
    context_object_name = "blog_post"

    def get_queryset(self):
        queryset = super().get_queryset().select_related("author")
        user = self.request.user
        if not user.is_staff and not user.is_superuser:
            queryset = queryset.filter(status=BlogPostStatus.PUBLISHED)
        return queryset

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Resources"),
                "url": reverse_lazy("marketing:resources"),
            },
        )
        breadcrumbs.append(
            {
                "name": _("Blog"),
                "url": reverse_lazy("marketing:blog:blog_list"),
            },
        )
        if getattr(self, "object", None):
            breadcrumbs.append(
                {
                    "name": self.object.title,
                    "url": "",
                },
            )
        return breadcrumbs

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        blog_post: BlogPost = self.object
        summary_source = (
            getattr(blog_post, "excerpt", None)
            or getattr(blog_post, "summary", None)
            or blog_post.content
        )
        page_subtitle = strip_tags(summary_source).strip() if summary_source else ""

        request = self.request
        absolute_url = request.build_absolute_uri(blog_post.get_absolute_url())
        meta_description = (
            blog_post.summary.strip()
            if blog_post.summary
            else blog_post.get_content_preview()
        )
        meta_description = (meta_description or "").strip()
        if not meta_description:
            meta_description = blog_post.title
        meta_description = meta_description[:300]

        share_image_url = None
        share_image_alt = None
        if blog_post.has_featured_image():
            candidate = blog_post.get_featured_image_url()
            if candidate:
                share_image_url = request.build_absolute_uri(candidate)
                share_image_alt = blog_post.get_featured_image_alt()
        if not share_image_url:
            default_path = MarketingShareImage.DEFAULT.value
            share_image_url = request.build_absolute_uri(static(default_path))
            share_image_alt = _(
                "Validibot robot showcasing workflow automation.",
            )

        context.update(
            {
                "section": "blog",
                "page_title": blog_post.title,
                "page_subtitle": page_subtitle[:180],
                "related_posts": self._get_related_posts(blog_post),
                "canonical_url": absolute_url,
                "meta_description": meta_description,
                "full_meta_title": _("{title} | Validibot Blog").format(
                    title=blog_post.title,
                ),
                "share_image_url": share_image_url,
                "share_image_alt": share_image_alt,
            },
        )
        return context

    def _get_related_posts(self, blog_post: BlogPost):
        queryset = BlogPost.objects.exclude(pk=blog_post.pk).order_by("-published_on")
        user = self.request.user
        if not user.is_staff and not user.is_superuser:
            queryset = queryset.filter(status=BlogPostStatus.PUBLISHED)
        return queryset[:3]

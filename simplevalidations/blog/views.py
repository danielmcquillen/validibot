# Create your views here.
from typing import Any

from django.urls import reverse_lazy
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _
from django.views import generic

from simplevalidations.blog.models import BlogPost
from simplevalidations.core.mixins import BreadcrumbMixin


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
            queryset = queryset.filter(status=1)
        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "section": "blog",
                "page_title": _("Blog"),
                "page_subtitle": _(
                    "Insights, guides, and product updates from the "
                    "SimpleValidations team.",
                ),
                "has_drafts": context["blog_posts"].filter(status=0).exists()
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
            queryset = queryset.filter(status=1)
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

        context.update(
            {
                "section": "blog",
                "page_title": blog_post.title,
                "page_subtitle": page_subtitle[:180],
                "related_posts": self._get_related_posts(blog_post),
            },
        )
        return context

    def _get_related_posts(self, blog_post: BlogPost):
        queryset = BlogPost.objects.exclude(pk=blog_post.pk).order_by("-published_on")
        user = self.request.user
        if not user.is_staff and not user.is_superuser:
            queryset = queryset.filter(status=1)
        return queryset[:3]

# Create your views here.

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from roscoe.core.mixins import BreadcrumbMixin


class HomePageView(TemplateView):
    template_name = "marketing/home.html"
    http_method_names = ["get"]


class AboutPageView(BreadcrumbMixin, TemplateView):
    template_name = "marketing/about.html"
    http_method_names = ["get"]
    breadcrumbs = [
        {"name": _("About"), "url": ""},
    ]


class FeaturesPageView(BreadcrumbMixin, TemplateView):
    template_name = "marketing/features.html"
    http_method_names = ["get"]
    breadcrumbs = [
        {"name": _("Features"), "url": ""},
    ]


class PricingPageView(BreadcrumbMixin, TemplateView):
    template_name = "marketing/pricing.html"
    http_method_names = ["get"]
    breadcrumbs = [
        {"name": _("Pricing"), "url": ""},
    ]


class ResourcesPageView(BreadcrumbMixin, TemplateView):
    template_name = "marketing/resources.html"
    http_method_names = ["get"]
    breadcrumbs = [
        {"name": _("Resources"), "url": ""},
    ]


class ResourceDetailPageView(BreadcrumbMixin, TemplateView):
    """Base class for resource sub-pages to keep breadcrumbs consistent."""

    page_title: str = ""

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Resources"),
                "url": reverse_lazy("marketing:resources"),
            },
        )
        breadcrumbs.append({"name": _(self.page_title), "url": ""})
        return breadcrumbs


class DocsPageView(ResourceDetailPageView):
    template_name = "marketing/resources_docs.html"
    http_method_names = ["get"]
    page_title = "Docs"


class BlogPageView(ResourceDetailPageView):
    template_name = "marketing/resources_blog.html"
    http_method_names = ["get"]
    page_title = "Blog"


class YoutubePageView(ResourceDetailPageView):
    template_name = "marketing/resources_youtube.html"
    http_method_names = ["get"]
    page_title = "YouTube"


class ChangelogPageView(ResourceDetailPageView):
    template_name = "marketing/resources_changelog.html"
    http_method_names = ["get"]
    page_title = "Changelog"


class FAQPageView(BreadcrumbMixin, TemplateView):
    template_name = "marketing/faq.html"
    http_method_names = ["get"]
    breadcrumbs = [
        {"name": _("FAQ"), "url": ""},
    ]


class SupportDetailPageView(BreadcrumbMixin, TemplateView):
    page_title: str = ""

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append({"name": _("Support"), "url": ""})
        breadcrumbs.append({"name": _(self.page_title), "url": ""})
        return breadcrumbs


class ContactPageView(SupportDetailPageView):
    template_name = "marketing/contact.html"
    http_method_names = ["get"]
    page_title = "Contact Us"


class HelpCenterPageView(SupportDetailPageView):
    template_name = "marketing/help_center.html"
    http_method_names = ["get"]
    page_title = "Help Center"


class StatusPageView(SupportDetailPageView):
    template_name = "marketing/status.html"
    http_method_names = ["get"]
    page_title = "Status"


class TermsPageView(BreadcrumbMixin, TemplateView):
    template_name = "marketing/terms.html"
    http_method_names = ["get"]
    breadcrumbs = [
        {"name": _("Terms of Service"), "url": ""},
    ]


class PrivacyPageView(BreadcrumbMixin, TemplateView):
    template_name = "marketing/privacy.html"
    http_method_names = ["get"]
    breadcrumbs = [
        {"name": _("Privacy Policy"), "url": ""},
    ]

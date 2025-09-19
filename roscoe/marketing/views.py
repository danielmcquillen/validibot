# Create your views here.

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from roscoe.core.forms import SupportMessageForm
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


class FeatureDetailPageView(BreadcrumbMixin, TemplateView):
    template_name: str = ""
    http_method_names = ["get"]
    page_title: str = ""

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Features"),
                "url": reverse_lazy("marketing:features"),
            },
        )
        breadcrumbs.append({"name": _(self.page_title), "url": ""})
        return breadcrumbs


class FeatureOverviewPageView(FeatureDetailPageView):
    template_name = "marketing/features/overview.html"
    page_title = "Overview"


class FeatureSchemaValidationPageView(FeatureDetailPageView):
    template_name = "marketing/features/schema_validation.html"
    page_title = "Schema Validation"


class FeatureSimulationValidationPageView(FeatureDetailPageView):
    template_name = "marketing/features/simulation_validation.html"
    page_title = "Simulation Validation"


class FeatureCertificatesPageView(FeatureDetailPageView):
    template_name = "marketing/features/certificates.html"
    page_title = "Certificates"


class FeatureBlockchainPageView(FeatureDetailPageView):
    template_name = "marketing/features/blockchain.html"
    page_title = "Blockchain"


class FeatureGithubIntegrationPageView(FeatureDetailPageView):
    template_name = "marketing/features/integrations/github.html"
    page_title = "GitHub Integration"


class FeatureSlackIntegrationPageView(FeatureDetailPageView):
    template_name = "marketing/features/integrations/slack.html"
    page_title = "Slack Integration"


class PricingPageView(BreadcrumbMixin, TemplateView):
    template_name = "marketing/pricing.html"
    http_method_names = ["get"]
    breadcrumbs = [
        {"name": _("Pricing"), "url": ""},
    ]


class PricingDetailPageView(BreadcrumbMixin, TemplateView):
    template_name: str = ""
    http_method_names = ["get"]
    page_title: str = ""

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Pricing"),
                "url": reverse_lazy("marketing:pricing"),
            },
        )
        breadcrumbs.append({"name": _(self.page_title), "url": ""})
        return breadcrumbs


class PricingStarterPageView(PricingDetailPageView):
    template_name = "marketing/pricing/starter.html"
    page_title = "Starter"


class PricingGrowthPageView(PricingDetailPageView):
    template_name = "marketing/pricing/growth.html"
    page_title = "Growth"


class PricingEnterprisePageView(PricingDetailPageView):
    template_name = "marketing/pricing/enterprise.html"
    page_title = "Enterprise"


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


class YoutubePageView(ResourceDetailPageView):
    template_name = "marketing/resources_youtube.html"
    http_method_names = ["get"]
    page_title = "YouTube"


class ChangelogPageView(ResourceDetailPageView):
    template_name = "marketing/resources_changelog.html"
    http_method_names = ["get"]
    page_title = "Changelog"


class FAQPageView(ResourceDetailPageView):
    template_name = "marketing/faq.html"
    http_method_names = ["get"]
    page_title = "FAQ"


class SupportDetailPageView(BreadcrumbMixin, TemplateView):
    page_title: str = ""

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Support"),
                "url": reverse_lazy("marketing:support"),
            },
        )
        breadcrumbs.append({"name": _(self.page_title), "url": ""})
        return breadcrumbs


class SupportHomePageView(BreadcrumbMixin, TemplateView):
    template_name = "marketing/support.html"
    http_method_names = ["get"]
    breadcrumbs = [
        {"name": _("Support"), "url": ""},
    ]


class ContactPageView(SupportDetailPageView):
    template_name = "marketing/contact.html"
    http_method_names = ["get"]
    page_title = "Contact Us"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            context.setdefault("support_message_form", SupportMessageForm())
        return context


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

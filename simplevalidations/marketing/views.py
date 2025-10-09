import json

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView

from simplevalidations.core.forms import SupportMessageForm
from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.core.utils import is_htmx
from simplevalidations.marketing.forms import BetaWaitlistForm
from simplevalidations.marketing.models import Prospect
from simplevalidations.marketing.services import (
    WaitlistPayload,
    WaitlistSignupError,
    submit_waitlist_signup,
)


class HomePageView(TemplateView):
    template_name = "marketing/home.html"
    http_method_names = ["get"]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault(
            "waitlist_form",
            BetaWaitlistForm(initial={"origin": BetaWaitlistForm.ORIGIN_HERO}),
        )
        return context


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


class VideosPageView(ResourceDetailPageView):
    template_name = "marketing/resources_videos.html"
    http_method_names = ["get"]
    page_title = "Videos"


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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            context.setdefault("support_message_form", SupportMessageForm())
        return context


class ContactPageView(SupportDetailPageView):
    template_name = "marketing/contact.html"
    http_method_names = ["get"]
    page_title = "Contact Us"


@require_http_methods(["POST"])
def submit_beta_waitlist(request: HttpRequest) -> HttpResponse:
    form = BetaWaitlistForm(request.POST)
    if form.is_valid():
        origin = form.cleaned_data["origin"]
        source = (
            "marketing_footer"
            if origin == BetaWaitlistForm.ORIGIN_FOOTER
            else "marketing_homepage"
        )
        payload = WaitlistPayload(
            email=form.cleaned_data["email"],
            metadata={
                "source": source,
                "origin": origin,
                "user_agent": request.META.get("HTTP_USER_AGENT"),
                "ip": request.META.get("REMOTE_ADDR"),
                "referer": request.META.get("HTTP_REFERER"),
            },
        )
        try:
            submit_waitlist_signup(payload)
        except WaitlistSignupError:
            form.add_error(
                None,
                _(
                    "We couldn't add you to the waitlist just now. Please try again in a moment.",
                ),
            )
        else:
            success_context = {
                "headline": _("You're on the list!"),
                "body": _(
                    "Thanks for your interest. We'll email you as soon as the beta is ready.",
                ),
                "footer_message": _("Thanks! We'll be in touch soon."),
            }
            template_base = (
                "marketing/partial/footer_waitlist"
                if origin == BetaWaitlistForm.ORIGIN_FOOTER
                else "marketing/partial/waitlist"
            )
            if is_htmx(request):
                return render(
                    request,
                    f"{template_base}_success.html",
                    success_context,
                    status=201,
                )
            messages.success(request, success_context["body"])
            return redirect(reverse("marketing:home"))

    if is_htmx(request):
        origin = request.POST.get("origin", BetaWaitlistForm.ORIGIN_HERO)
        if origin not in BetaWaitlistForm.ALLOWED_ORIGINS:
            origin = BetaWaitlistForm.ORIGIN_HERO
        template_base = (
            "marketing/partial/footer_waitlist"
            if origin == BetaWaitlistForm.ORIGIN_FOOTER
            else "marketing/partial/waitlist"
        )
        return render(
            request,
            f"{template_base}_form.html",
            {"form": form},
            status=400,
        )

    messages.error(
        request,
        _(
            "We couldn't add you to the waitlist. Please correct the highlighted fields and try again.",
        ),
    )
    return redirect(reverse("marketing:home"))


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


@csrf_exempt
@require_http_methods(["POST"])
def postmark_delivery_webhook(request: HttpRequest) -> HttpResponse:
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if payload.get("RecordType") == "Delivery":
        email = payload.get("Recipient") or payload.get("Email")
        if email:
            Prospect.objects.filter(
                email=email,
                email_status=Prospect.EmailStatus.PENDING,
            ).update(email_status=Prospect.EmailStatus.VERIFIED)
    return HttpResponse(status=200)


@csrf_exempt
@require_http_methods(["POST"])
def postmark_bounce_webhook(request: HttpRequest) -> HttpResponse:
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if payload.get("RecordType") == "Bounce" and payload.get("Type") == "HardBounce":
        email = payload.get("Email") or payload.get("Recipient")
        if email:
            Prospect.objects.filter(email=email).update(
                email_status=Prospect.EmailStatus.INVALID,
            )
    return HttpResponse(status=200)

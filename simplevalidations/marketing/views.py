import json

from django.contrib import messages
from django.contrib.sites.models import Site
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
from simplevalidations.marketing.constants import ProspectEmailStatus
from simplevalidations.marketing.email.utils import is_allowed_postmark_source
from simplevalidations.marketing.forms import BetaWaitlistForm
from simplevalidations.marketing.models import Prospect
from simplevalidations.marketing.services import (
    WaitlistPayload,
    WaitlistSignupError,
    submit_waitlist_signup,
)


class MarketingMetadataMixin:
    page_title: str | None = None
    meta_description: str = _(
        "SimpleValidations helps teams automate data quality checks, run complex validations, and certify results with confidence.",
    )
    meta_keywords: str = "data validation, AI validation, simulation validation, credential automation"

    def get_page_title(self) -> str | None:
        return self.page_title

    def get_meta_description(self) -> str:
        return self.meta_description

    def get_meta_keywords(self) -> str:
        return self.meta_keywords

    def get_structured_data(self, site_origin: str, canonical_url: str) -> list[dict]:
        waitlist_url = f"{site_origin}{reverse('marketing:beta_waitlist')}"
        organization = {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": "McQuillen Interactive",
            "url": site_origin,
        }
        website = {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "SimpleValidations",
            "url": site_origin,
            "description": self.get_meta_description(),
            "potentialAction": {
                "@type": "SubscribeAction",
                "target": waitlist_url,
                "name": "Join the SimpleValidations beta waitlist",
            },
        }
        webpage = {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": self.get_page_title() or "SimpleValidations",
            "url": canonical_url,
            "description": self.get_meta_description(),
        }
        return [organization, website, webpage]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page_title = self.get_page_title()
        if page_title is not None:
            context.setdefault("page_title", page_title)
        context.setdefault("meta_description", self.get_meta_description())
        context.setdefault("meta_keywords", self.get_meta_keywords())

        request = getattr(self, "request", None)
        if request:
            canonical = request.build_absolute_uri()
            site_origin = request.build_absolute_uri("/").rstrip("/")

            context.setdefault("canonical_url", canonical)
            context.setdefault("site_origin", site_origin)

            structured_data = self.get_structured_data(site_origin, canonical)
            if structured_data:
                context["structured_data_json"] = json.dumps(
                    structured_data,
                    ensure_ascii=False,
                )
        return context


class HomePageView(MarketingMetadataMixin, TemplateView):
    template_name = "marketing/home.html"
    http_method_names = ["get"]
    page_title = _("Meet Your Data Validation Assistant")
    meta_description = _(
        "SimpleValidations pairs deterministic checks, AI review, and simulations so every document is production-ready before it reaches your customers.",
    )
    meta_keywords = (
        "data validation assistant, data quality automation, simulation validation"
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault(
            "waitlist_form",
            BetaWaitlistForm(origin=BetaWaitlistForm.ORIGIN_HERO),
        )
        return context


class AboutPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name = "marketing/about.html"
    http_method_names = ["get"]
    page_title = _("About SimpleValidations")
    meta_description = _(
        "Learn about Daniel McQuillen and the story behind SimpleValidations, inspired by mission-critical validation work at Lawrence Berkeley National Laboratory.",
    )
    breadcrumbs = [
        {"name": _("About"), "url": ""},
    ]


class FeaturesPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name = "marketing/features.html"
    http_method_names = ["get"]
    page_title = _("Feature Tour")
    meta_description = _(
        "Explore how SimpleValidations blends schema checks, simulations, and credentialing to keep every submission trustworthy.",
    )
    breadcrumbs = [
        {"name": _("Features"), "url": ""},
    ]


class FeatureDetailPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name: str = ""
    http_method_names = ["get"]
    page_title: str = ""
    meta_description: str = _(
        "Dive into SimpleValidations capabilities with in-depth feature briefings for technical teams.",
    )

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
    page_title = _("Platform Overview")
    meta_description = _(
        "See how SimpleValidations orchestrates AI, simulations, and human-friendly workflows to deliver trustworthy data pipelines.",
    )


class FeatureSchemaValidationPageView(FeatureDetailPageView):
    template_name = "marketing/features/schema_validation.html"
    page_title = _("Schema Validation")
    meta_description = _(
        "Build rigorous schema validation with reusable checks, contextual errors, and collaborative review loops.",
    )


class FeatureSimulationValidationPageView(FeatureDetailPageView):
    template_name = "marketing/features/simulation_validation.html"
    page_title = _("Simulation Validation")
    meta_description = _(
        "Blend deterministic rules with simulations or complex domain logic to verify results against real-world scenarios.",
    )


class FeatureCertificatesPageView(FeatureDetailPageView):
    template_name = "marketing/features/certificates.html"
    page_title = _("Credential Issuance")
    meta_description = _(
        "Issue certificates and compliance artifacts automatically once a submission passes every validation checkpoint.",
    )


class FeatureBlockchainPageView(FeatureDetailPageView):
    template_name = "marketing/features/blockchain.html"
    page_title = _("Blockchain Provenance")
    meta_description = _(
        "Track validation provenance on an immutable ledger to give regulators and customers tamper-evident confidence.",
    )


class FeatureIntegrationsPageView(FeatureDetailPageView):
    template_name = "marketing/features/integrations.html"
    page_title = _("Integrations")
    meta_description = _(
        "Connect SimpleValidations to your stack with webhooks, REST APIs, and export-ready payloads.",
    )


class PricingPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name = "marketing/pricing.html"
    http_method_names = ["get"]
    page_title = _("Pricing")
    meta_description = _(
        "Compare SimpleValidations pricing plans for growing teams that need dependable data quality.",
    )
    breadcrumbs = [
        {"name": _("Pricing"), "url": ""},
    ]


class PricingDetailPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name: str = ""
    http_method_names = ["get"]
    page_title: str = ""
    meta_description: str = _(
        "Select the SimpleValidations plan that aligns with your team's scale, automation goals, and support needs.",
    )

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
    origin = (request.POST.get("origin") or BetaWaitlistForm.ORIGIN_HERO).strip().lower()
    if origin not in BetaWaitlistForm.ALLOWED_ORIGINS:
        origin = BetaWaitlistForm.ORIGIN_HERO

    form = BetaWaitlistForm(
        request.POST,
        origin=origin,
        target_id=BetaWaitlistForm.FORM_TARGETS.get(origin),
    )
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
    if not is_allowed_postmark_source(request):
        return HttpResponse(status=403)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if payload.get("RecordType") == "Delivery":
        email = payload.get("Recipient") or payload.get("Email")
        if email:
            Prospect.objects.filter(
                email=email,
                email_status=ProspectEmailStatus.PENDING,
            ).update(email_status=ProspectEmailStatus.VERIFIED)
    return HttpResponse(status=200)


@csrf_exempt
@require_http_methods(["POST"])
def postmark_bounce_webhook(request: HttpRequest) -> HttpResponse:
    if not is_allowed_postmark_source(request):
        return HttpResponse(status=403)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if payload.get("RecordType") == "Bounce" and payload.get("Type") == "HardBounce":
        email = payload.get("Email") or payload.get("Recipient")
        if email:
            Prospect.objects.filter(email=email).update(
                email_status=ProspectEmailStatus.INVALID,
            )
    return HttpResponse(status=200)

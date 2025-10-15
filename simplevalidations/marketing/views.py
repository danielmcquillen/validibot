import json

from django.conf import settings
from django.contrib import messages
from django.contrib.sites.models import Site
from django.http import HttpRequest
from django.http import HttpResponse
from django.shortcuts import redirect
from django.shortcuts import render
from django.templatetags.static import static
from django.urls import reverse
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.views.generic import TemplateView

from simplevalidations.core.forms import SupportMessageForm
from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.core.utils import is_htmx
from simplevalidations.marketing.constants import MarketingShareImage
from simplevalidations.marketing.constants import ProspectEmailStatus
from simplevalidations.marketing.email.utils import is_allowed_postmark_source
from simplevalidations.marketing.forms import BetaWaitlistForm
from simplevalidations.marketing.models import Prospect
from simplevalidations.marketing.services import WaitlistPayload
from simplevalidations.marketing.services import WaitlistSignupError
from simplevalidations.marketing.services import submit_waitlist_signup


class MarketingMetadataMixin:
    page_title: str | None = None
    meta_description: str = _(
        "SimpleValidations helps teams automate data quality checks, run complex "
        "validations, and certify results with confidence.",
    )
    meta_keywords: str = (
        "data validation, AI validation, simulation validation, credential automation"
    )
    share_image_path: str | MarketingShareImage | None = MarketingShareImage.DEFAULT
    share_image_alt: str | None = _(
        "Illustration of the SimpleValidations robot guiding teams through workflow automation.",
    )

    def get_page_title(self) -> str | None:
        return self.page_title

    def get_meta_description(self) -> str:
        return str(self.meta_description)

    def get_meta_keywords(self) -> str:
        return str(self.meta_keywords)

    def get_share_image_path(self) -> str | None:
        image_path = self.share_image_path
        if image_path is None:
            return None
        if isinstance(image_path, MarketingShareImage):
            return image_path.value
        return str(image_path)

    def get_share_image_alt(self) -> str | None:
        if self.share_image_alt is None:
            return None
        return str(self.share_image_alt)

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
        description = str(self.get_meta_description())
        page_name = str(self.get_page_title() or "SimpleValidations")
        keywords = str(self.get_meta_keywords())

        webpage = {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": page_name,
            "url": canonical_url,
            "description": description,
            "keywords": keywords,
        }
        return [organization, website, webpage]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page_title = self.get_page_title()
        brand_name = "SimpleValidations"
        if page_title is not None:
            title_value = str(page_title)
            context.setdefault("page_title", title_value)
            context.setdefault("full_meta_title", f"{title_value} | {brand_name}")
        else:
            context.setdefault("full_meta_title", brand_name)
        context.setdefault("meta_description", str(self.get_meta_description()))
        context.setdefault("meta_keywords", str(self.get_meta_keywords()))

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

            share_image_path = self.get_share_image_path()
            if share_image_path:
                if share_image_path.startswith(("http://", "https://")):
                    share_image_url = share_image_path
                else:
                    share_image_url = request.build_absolute_uri(
                        static(share_image_path)
                    )
                context.setdefault("share_image_url", share_image_url)
                share_image_alt = self.get_share_image_alt()
                if share_image_alt:
                    context.setdefault("share_image_alt", share_image_alt)
        return context


class HomePageView(MarketingMetadataMixin, TemplateView):
    template_name = "marketing/home.html"
    http_method_names = ["get"]
    page_title = _("Meet Your Data Validation Assistant")

    if settings.ENABLE_AI_VALIDATIONS:
        meta_description = _(
            "SimpleValidations lets you build robust data validation workflows with "
            "schema checks, simulations, AI review, and credentialing.",
        )
    else:
        meta_description = _(
            "SimpleValidations lets you build robust data validation workflows with "
            "schema checks, simulations, and credentialing.",
        )
    meta_keywords = (
        "data validation assistant, data quality automation, simulation validation"
    )
    share_image_path = MarketingShareImage.DEFAULT
    share_image_alt = _(
        "SimpleValidations robot greeting teams beside an abstract workflow diagram",
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
        "Learn about Daniel McQuillen and the story behind SimpleValidations",
    )
    breadcrumbs = []


class FeaturesPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name = "marketing/features.html"
    http_method_names = ["get"]
    page_title = _("Feature Tour")
    meta_description = _(
        "Explore how SimpleValidations blends schema checks, simulations, "
        "and credentialing to keep every submission trustworthy.",
    )
    breadcrumbs = []
    share_image_path = MarketingShareImage.DEFAULT
    share_image_alt = _(
        "SimpleValidations robot showcasing the platform's core feature set.",
    )


class FeatureDetailPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name: str = ""
    http_method_names = ["get"]
    page_title: str = ""
    meta_description: str = _(
        "Dive into SimpleValidations capabilities with "
        "in-depth feature briefings for technical teams.",
    )
    share_image_path: str | MarketingShareImage | None = MarketingShareImage.DEFAULT

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
    page_title = _("SimpleValidations Overview")
    meta_description = _(
        "See how SimpleValidations orchestrates AI, simulations, "
        "and human-friendly workflows to deliver trustworthy data pipelines.",
    )
    share_image_path = MarketingShareImage.DEFAULT
    share_image_alt = _(
        "SimpleValidations robot overview illustration spanning workflow layers.",
    )


class FeatureSchemaValidationPageView(FeatureDetailPageView):
    template_name = "marketing/features/schema_validation.html"
    page_title = _("Schema Validation")
    meta_description = _(
        "Build rigorous schema validation with reusable checks, "
        "contextual errors, and collaborative review loops.",
    )
    share_image_path = MarketingShareImage.SCHEMA_VALIDATION
    share_image_alt = _(
        "Robot analyst reviewing schema validation results on layered dashboards.",
    )


class FeatureSimulationValidationPageView(FeatureDetailPageView):
    template_name = "marketing/features/simulation_validation.html"
    page_title = _("Simulation Validation")
    meta_description = _(
        "Blend deterministic rules with simulations or complex domain "
        "logic to verify results against real-world scenarios.",
    )
    share_image_path = MarketingShareImage.SIMULATION_VALIDATION
    share_image_alt = _(
        "Robot running simulation experiments across validation terminals.",
    )


class FeatureCertificatesPageView(FeatureDetailPageView):
    template_name = "marketing/features/certificates.html"
    page_title = _("Credential Issuance")
    meta_description = _(
        "Issue certificates and compliance artifacts automatically "
        "once a submission passes every validation checkpoint.",
    )
    share_image_path = MarketingShareImage.CERTIFICATES
    share_image_alt = _(
        "Robot presenting issued compliance certificates after successful validations.",
    )


class FeatureBlockchainPageView(FeatureDetailPageView):
    template_name = "marketing/features/blockchain.html"
    page_title = _("Blockchain")
    meta_description = _(
        "Track validation on an immutable blockchain to give "
        "regulators and customers tamper-evident confidence.",
    )
    share_image_path = MarketingShareImage.BLOCKCHAIN
    share_image_alt = _(
        "Robot anchoring validation proofs onto a glowing blockchain ledger.",
    )


class FeatureIntegrationsPageView(FeatureDetailPageView):
    template_name = "marketing/features/integrations.html"
    page_title = _("Integrations")
    meta_description = _(
        "Connect SimpleValidations to your stack with webhooks, "
        "REST APIs, and export-ready payloads.",
    )
    share_image_path = MarketingShareImage.INTEGRATIONS
    share_image_alt = _(
        "Robot coordinating integrations between GitHub, Slack, and validation workflows.",
    )


class PricingPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name = "marketing/pricing.html"
    http_method_names = ["get"]
    page_title = _("Pricing")
    meta_description = _(
        "Compare SimpleValidations pricing plans for growing teams "
        "that need dependable data quality.",
    )
    breadcrumbs = []


class PricingDetailPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name: str = ""
    http_method_names = ["get"]
    page_title: str = ""
    meta_description: str = _(
        "Select the SimpleValidations plan that aligns with your "
        "team's scale, automation goals, and support needs.",
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
    page_title = _("Starter Plan")
    meta_description = _(
        "Starter brings automated validation and credentialing to "
        "lean teams launching their first workflows.",
    )


class PricingGrowthPageView(PricingDetailPageView):
    template_name = "marketing/pricing/growth.html"
    page_title = _("Growth Plan")
    meta_description = _(
        "Growth adds collaboration tooling and advanced automation "
        "for teams scaling complex validation programs.",
    )


class PricingEnterprisePageView(PricingDetailPageView):
    template_name = "marketing/pricing/enterprise.html"
    page_title = _("Enterprise Plan")
    meta_description = _(
        "Enterprise delivers custom SLAs, integrations, and governance "
        "controls for mission-critical validation.",
    )


class ResourcesPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name = "marketing/resources.html"
    http_method_names = ["get"]
    page_title = _("Resource Library")
    meta_description = _(
        "Browse documentation, videos, and changelog highlights "
        "to get the most out of SimpleValidations.",
    )
    breadcrumbs = []


class ResourceDetailPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
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
    page_title = _("Documentation")
    meta_description = _(
        "Read the SimpleValidations documentation to understand architecture, "
        "APIs, and implementation patterns.",
    )


class VideosPageView(ResourceDetailPageView):
    template_name = "marketing/resources_videos.html"
    http_method_names = ["get"]
    page_title = _("Video Library")
    meta_description = _(
        "Watch product walkthroughs and best-practice videos for "
        "SimpleValidations deployments.",
    )


class ChangelogPageView(ResourceDetailPageView):
    template_name = "marketing/resources_changelog.html"
    http_method_names = ["get"]
    page_title = _("Changelog")
    meta_description = _(
        "See what shipped recently across the SimpleValidations platform.",
    )


class FAQPageView(ResourceDetailPageView):
    template_name = "marketing/faq.html"
    http_method_names = ["get"]
    page_title = _("Frequently Asked Questions")
    meta_description = _(
        "Find answers to common questions about SimpleValidations "
        "setup, automation, and support.",
    )


class SupportDetailPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    page_title: str = ""
    meta_description: str = _(
        "Get help from the SimpleValidations team through support "
        "guides, contact forms, and system status updates.",
    )

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


class SupportHomePageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name = "marketing/support.html"
    http_method_names = ["get"]
    page_title = _("Support")
    meta_description = _(
        "Access support resources and contact options for the SimpleValidations team.",
    )
    breadcrumbs = []

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            context.setdefault("support_message_form", SupportMessageForm())
        return context


class ContactPageView(SupportDetailPageView):
    template_name = "marketing/contact.html"
    http_method_names = ["get"]
    page_title = _("Contact Us")
    meta_description = _(
        "Reach out to SimpleValidations for product questions, "
        "partnerships, or support escalations.",
    )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            context.setdefault("support_message_form", SupportMessageForm())
        return context

    def get_breadcrumbs(self):
        if not settings.ENABLE_HELP_CENTER and not settings.ENABLE_SYSTEM_STATUS:
            return []
        return super().get_breadcrumbs()


@require_http_methods(["POST"])
def submit_beta_waitlist(request: HttpRequest) -> HttpResponse:
    origin = (
        (request.POST.get("origin") or BetaWaitlistForm.ORIGIN_HERO).strip().lower()
    )
    if origin not in BetaWaitlistForm.ALLOWED_ORIGINS:
        origin = BetaWaitlistForm.ORIGIN_HERO

    form = BetaWaitlistForm(
        request.POST,
        origin=origin,
        target_id=BetaWaitlistForm.FORM_TARGETS.get(origin),
    )
    if form.is_valid():
        origin = form.cleaned_data["origin"]
        email = form.cleaned_data["email"]
        source = (
            "marketing_footer"
            if origin == BetaWaitlistForm.ORIGIN_FOOTER
            else "marketing_homepage"
        )
        metadata: dict[str, str | None] = {
            "source": source,
            "origin": origin,
            "user_agent": request.META.get("HTTP_USER_AGENT"),
            "ip": request.META.get("REMOTE_ADDR"),
            "referer": request.META.get("HTTP_REFERER"),
        }

        existing_prospect = Prospect.objects.filter(email=email).exists()
        if existing_prospect:
            metadata["skip_email"] = True

        payload = WaitlistPayload(
            email=email,
            metadata=metadata,
        )
        try:
            submit_waitlist_signup(payload)
        except WaitlistSignupError:
            form.add_error(
                None,
                _(
                    "We couldn't add you to the waitlist just now. "
                    "Please try again in a moment.",
                ),
            )
        else:
            success_context = {
                "headline": _("You're on the list!"),
                "body": _(
                    "Thanks for your interest. "
                    "We'll email you as soon as the beta is ready.",
                ),
                "footer_message": _("Thanks! We'll be in touch soon."),
            }
            if existing_prospect:
                success_context["body"] = _(
                    "Looks like you're already on the beta list — "
                    "we'll keep you posted.",
                )
                success_context["footer_message"] = _(
                    "You're already signed up — thanks for staying tuned!",
                )
            template_base = (
                "marketing/partial/footer_waitlist"
                if origin == BetaWaitlistForm.ORIGIN_FOOTER
                else "marketing/partial/waitlist"
            )
            if is_htmx(request):
                status_code = 200 if existing_prospect else 201
                return render(
                    request,
                    f"{template_base}_success.html",
                    success_context,
                    status=status_code,
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
            "We couldn't add you to the waitlist. "
            "Please correct the highlighted fields and try again.",
        ),
    )
    return redirect(reverse("marketing:home"))


class HelpCenterPageView(SupportDetailPageView):
    template_name = "marketing/help_center.html"
    http_method_names = ["get"]
    page_title = _("Help Center")
    meta_description = _(
        "Browse help center articles for troubleshooting and workflow guidance.",
    )


class StatusPageView(SupportDetailPageView):
    template_name = "marketing/status.html"
    http_method_names = ["get"]
    page_title = _("System Status")
    meta_description = _(
        "Check the latest SimpleValidations platform uptime and incident history.",
    )


class TermsPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name = "marketing/terms.html"
    http_method_names = ["get"]
    page_title = _("Terms of Service")
    meta_description = _(
        "Review the SimpleValidations terms of service "
        "covering platform usage and responsibilities.",
    )
    breadcrumbs = []


class PrivacyPageView(MarketingMetadataMixin, BreadcrumbMixin, TemplateView):
    template_name = "marketing/privacy.html"
    http_method_names = ["get"]
    page_title = _("Privacy Policy")
    meta_description = _(
        "Understand how SimpleValidations collects, "
        "processes, and protects personal data.",
    )
    breadcrumbs = []


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


def robots_txt(request: HttpRequest) -> HttpResponse:
    site = Site.objects.get_current(request)
    scheme = request.scheme or "https"
    origin = f"{scheme}://{site.domain}".rstrip("/")
    lines = [
        "User-agent: *",
        "Allow: /",
        f"Sitemap: {origin}/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")

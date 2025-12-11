from __future__ import annotations

import logging
import typing

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.urls import reverse

if typing.TYPE_CHECKING:
    from allauth.socialaccount.models import SocialLogin
    from django.http import HttpRequest

    from validibot.users.models import User

logger = logging.getLogger(__name__)

# Session key for storing the selected plan during signup flow
SELECTED_PLAN_SESSION_KEY = "signup_selected_plan"


def get_selected_plan_from_session(request: HttpRequest) -> dict | None:
    """
    Get the selected plan details from session for template context.

    Returns a dict with plan details if a valid plan is selected,
    or None if no plan is selected.
    """
    plan_code = request.session.get(SELECTED_PLAN_SESSION_KEY)
    if not plan_code:
        return None

    try:
        from validibot.billing.models import Plan

        plan = Plan.objects.filter(code=plan_code).first()
        if plan:
            return {
                "code": plan.code,
                "name": plan.name,
                "description": plan.description,
                "monthly_price_dollars": plan.monthly_price_dollars,
                "basic_launches_limit": plan.basic_launches_limit,
                "included_credits": plan.included_credits,
                "max_seats": plan.max_seats,
            }
    except Exception:
        logger.exception("Error fetching plan from session")

    return None


class AccountAdapter(DefaultAccountAdapter):
    """
    Custom account adapter for Validibot signup flow.

    Captures the selected plan from pricing page and redirects to checkout
    after signup to create a seamless pricing-to-trial-to-paid conversion.

    Flow:
    1. User clicks "Start free trial" on pricing page → /accounts/signup/?plan=TEAM
    2. Signup page shows plan context ("You selected Team plan")
    3. User signs up
    4. After signup, redirected to /app/billing/checkout/?plan=TEAM
    5. User starts trial (or pays immediately if opt-out trial)
    """

    def is_open_for_signup(self, request: HttpRequest) -> bool:
        return getattr(settings, "ACCOUNT_ALLOW_REGISTRATION", True)

    def get_login_redirect_url(self, request: HttpRequest) -> str:
        """
        Store plan in session when user lands on login/signup page.

        This captures the plan from the URL when user first arrives.
        """
        # Capture plan from GET params when landing on page
        plan_code = request.GET.get("plan")
        if plan_code:
            request.session[SELECTED_PLAN_SESSION_KEY] = plan_code

        return super().get_login_redirect_url(request)

    def pre_login(
        self,
        request: HttpRequest,
        user: User,
        **kwargs,
    ) -> str | None:
        """
        Capture plan selection before login completes.

        Called just before the user is logged in. Captures the plan
        parameter from the form submission (hidden input).

        Uses **kwargs for compatibility with different allauth versions.
        """
        # Capture plan from POST (hidden form field)
        plan_code = request.POST.get("plan")
        if plan_code:
            request.session[SELECTED_PLAN_SESSION_KEY] = plan_code

        return super().pre_login(request, user, **kwargs)

    def get_signup_redirect_url(self, request: HttpRequest) -> str:
        """
        Redirect to checkout if user selected a plan from pricing page.

        Creates a smooth flow: Pricing → Signup → Checkout → Trial
        Instead of: Pricing → Signup → Dashboard (loses purchase intent)

        Also stores the intended plan on the subscription for analytics
        and for pre-selecting on trial-expired page.
        """
        selected_plan = request.session.get(SELECTED_PLAN_SESSION_KEY)

        if selected_plan:
            # Clear the session key after use (one-time redirect)
            del request.session[SELECTED_PLAN_SESSION_KEY]

            # Validate the plan exists before redirecting
            from validibot.billing.constants import PlanCode

            valid_plans = {choice.value for choice in PlanCode}
            if selected_plan in valid_plans and selected_plan != PlanCode.ENTERPRISE:
                # Store the intended plan on the subscription for analytics
                self._store_intended_plan(request.user, selected_plan)

                # Redirect to checkout with the selected plan
                return f"{reverse('billing:checkout')}?plan={selected_plan}"

        # Default: redirect to standard login redirect
        return settings.LOGIN_REDIRECT_URL

    def _store_intended_plan(self, user: User, plan_code: str) -> None:
        """
        Store the intended plan on the user's subscription.

        Called after signup to record which plan the user selected
        from the pricing page, for analytics and UX improvements.
        """
        try:
            from validibot.billing.models import Plan

            # Get the user's current org subscription
            if not user.current_org:
                return

            subscription = getattr(user.current_org, "subscription", None)
            if not subscription:
                return

            # Get the intended plan and store it
            intended_plan = Plan.objects.filter(code=plan_code).first()
            if intended_plan:
                subscription.intended_plan = intended_plan
                subscription.save(update_fields=["intended_plan"])
                logger.info(
                    "Stored intended plan %s for org %s",
                    plan_code,
                    user.current_org.id,
                )
        except Exception:
            logger.exception("Failed to store intended plan")


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(
        self,
        request: HttpRequest,
        sociallogin: SocialLogin,
    ) -> bool:
        return getattr(settings, "ACCOUNT_ALLOW_REGISTRATION", True)

    def populate_user(
        self,
        request: HttpRequest,
        sociallogin: SocialLogin,
        data: dict[str, typing.Any],
    ) -> User:
        """
        Populates user information from social provider info.

        See: https://docs.allauth.org/en/latest/socialaccount/advanced.html#creating-and-populating-user-instances
        """
        user = super().populate_user(request, sociallogin, data)
        if not user.name:
            if name := data.get("name"):
                user.name = name
            elif first_name := data.get("first_name"):
                user.name = first_name
                if last_name := data.get("last_name"):
                    user.name += f" {last_name}"
        return user

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

# Session key for storing workflow invite token during signup flow
WORKFLOW_INVITE_SESSION_KEY = "workflow_invite_token"


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
        """
        Determine if signup is allowed for this request.

        Signup is allowed if:
        1. ACCOUNT_ALLOW_REGISTRATION is True (open registration), OR
        2. The user has a workflow invite token in their session (invite-only access)

        This enables invite-only signup: set ACCOUNT_ALLOW_REGISTRATION=False to
        block public signup, but users with workflow invite links can still register.
        """
        # Always allow signup if user has a workflow invite token
        if request.session.get(WORKFLOW_INVITE_SESSION_KEY):
            return True
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
        Redirect to appropriate destination after signup.

        Handles two flows:
        1. Workflow invite flow: Accept invite, redirect to workflow launch
        2. Standard pricing flow: Redirect to billing dashboard

        For workflow invites:
        - New user is created as a Workflow Guest (no personal workspace)
        - Invite is accepted, creating a WorkflowAccessGrant
        - User is redirected to the workflow launch page

        For standard signups:
        - User gets a personal workspace with trial subscription
        - Redirected to billing dashboard to view trial/subscribe
        """
        # Check for workflow invite token first
        invite_token = request.session.get(WORKFLOW_INVITE_SESSION_KEY)
        if invite_token:
            redirect_url = self._handle_workflow_invite_signup(request, invite_token)
            if redirect_url:
                return redirect_url

        # Standard pricing flow
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

                # Redirect to billing dashboard with welcome flag
                return f"{reverse('billing:dashboard')}?welcome=1&plan={selected_plan}"

        # Default: redirect to standard login redirect
        return settings.LOGIN_REDIRECT_URL

    def _handle_workflow_invite_signup(
        self,
        request: HttpRequest,
        invite_token: str,
    ) -> str | None:
        """
        Handle workflow invite acceptance after signup.

        Accepts the workflow invite, creating a WorkflowAccessGrant,
        and returns the redirect URL to the workflow launch page.

        Returns None if the invite is invalid, allowing fallback to
        normal signup flow.
        """
        from django.contrib import messages
        from django.utils.translation import gettext_lazy as _

        from validibot.workflows.models import WorkflowInvite

        # Clear the session key
        del request.session[WORKFLOW_INVITE_SESSION_KEY]

        try:
            invite = WorkflowInvite.objects.select_related("workflow").get(
                token=invite_token,
            )

            # Check if still valid
            invite.mark_expired_if_needed()
            if invite.status != WorkflowInvite.Status.PENDING:
                logger.warning(
                    "Workflow invite %s is no longer pending (status: %s)",
                    invite_token,
                    invite.status,
                )
                messages.warning(
                    request,
                    _("The workflow invite is no longer valid."),
                )
                return None

            # Accept the invite
            grant = invite.accept(user=request.user)

            # Send acceptance notification to the inviter
            from validibot.workflows.emails import send_workflow_invite_accepted_email

            send_workflow_invite_accepted_email(grant)

            messages.success(
                request,
                _(
                    "Welcome! You now have access to the workflow '%(name)s'. "
                    "You can run validations on this workflow."
                )
                % {"name": invite.workflow.name},
            )

            # Redirect to workflow launch page
            return reverse(
                "workflows:workflow_launch",
                kwargs={"pk": invite.workflow.pk},
            )

        except WorkflowInvite.DoesNotExist:
            logger.warning("Workflow invite not found: %s", invite_token)
            return None
        except ValueError as e:
            logger.warning("Failed to accept workflow invite: %s", e)
            messages.error(request, str(e))
            return None

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
        """
        Determine if social signup is allowed for this request.

        Same logic as AccountAdapter: allow if open registration OR if user
        has a workflow invite token in session.
        """
        # Always allow signup if user has a workflow invite token
        if request.session.get(WORKFLOW_INVITE_SESSION_KEY):
            return True
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

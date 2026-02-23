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

# Session key for storing workflow invite token during signup flow
WORKFLOW_INVITE_SESSION_KEY = "workflow_invite_token"

# Session key for storing cloud trial invite token during signup flow.
# Set by the cloud onboarding AcceptTrialInviteView, consumed after signup
# to activate the trial on the user's personal organization.
TRIAL_INVITE_SESSION_KEY = "trial_invite_token"


def _is_cloud_self_register() -> bool:
    """
    Check if the cloud layer is installed and self-registration is enabled.

    Returns True only when validibot-cloud is installed AND its CloudSettings
    has signup_mode set to SELF_REGISTER. Returns False if the cloud package
    is not installed (community/self-hosted mode) or if signup mode is
    INVITE_ONLY.
    """
    try:
        from validibot_cloud.onboarding.models import CloudSettings
    except ImportError:
        return False

    cloud_settings = CloudSettings.get_cloud_settings()
    return cloud_settings.signup_mode == "self_register"


class AccountAdapter(DefaultAccountAdapter):
    """
    Custom account adapter for Validibot signup flow.

    Handles two types of invite-based signups:
    1. Workflow invites: users get access to specific workflows
    2. Trial invites: users from the cloud onboarding flow get a trial org
    """

    def is_open_for_signup(self, request: HttpRequest) -> bool:
        """
        Determine if signup is allowed for this request.

        Signup is allowed if:
        1. ACCOUNT_ALLOW_REGISTRATION is True (open registration), OR
        2. The user has a workflow invite token in their session, OR
        3. The user has a trial invite token in their session (cloud), OR
        4. Cloud is installed with self-registration enabled

        This enables invite-only signup: set ACCOUNT_ALLOW_REGISTRATION=False to
        block public signup, but users with invite links can still register.
        """
        # Always allow signup if user has a workflow invite token
        if request.session.get(WORKFLOW_INVITE_SESSION_KEY):
            return True

        # Always allow signup if user has a trial invite token (cloud)
        if request.session.get(TRIAL_INVITE_SESSION_KEY):
            return True

        # Allow signup if cloud layer has self-registration enabled
        if _is_cloud_self_register():
            return True

        return getattr(settings, "ACCOUNT_ALLOW_REGISTRATION", True)

    def get_signup_redirect_url(self, request: HttpRequest) -> str:
        """
        Redirect to appropriate destination after signup.

        Handles two invite flows:
        1. Workflow invite: accept invite, redirect to workflow launch page
        2. Trial invite: activate trial on user's org, redirect to dashboard

        Otherwise, redirect to the default login redirect URL.
        """
        # Check for workflow invite token first
        invite_token = request.session.get(WORKFLOW_INVITE_SESSION_KEY)
        if invite_token:
            redirect_url = self._handle_workflow_invite_signup(request, invite_token)
            if redirect_url:
                return redirect_url

        # Check for trial invite token (cloud onboarding)
        trial_token = request.session.get(TRIAL_INVITE_SESSION_KEY)
        if trial_token:
            redirect_url = self._handle_trial_invite_signup(request, trial_token)
            if redirect_url:
                return redirect_url

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

    def _handle_trial_invite_signup(
        self,
        request: HttpRequest,
        trial_token: str,
    ) -> str | None:
        """
        Handle trial invite activation after signup.

        Delegates to the cloud onboarding service to activate the trial
        on the user's personal organization. Returns the redirect URL
        on success, or None to fall back to default redirect.
        """
        from django.contrib import messages
        from django.utils.translation import gettext_lazy as _

        # Clear the session key regardless of outcome
        del request.session[TRIAL_INVITE_SESSION_KEY]

        try:
            from validibot_cloud.onboarding.services import activate_trial_for_user
        except ImportError:
            logger.exception(
                "Trial invite token in session but validibot-cloud not installed",
            )
            return None

        try:
            activate_trial_for_user(request.user, trial_token)
        except Exception:
            logger.exception(
                "Failed to activate trial for user %s with token %s",
                request.user,
                trial_token,
            )
            messages.warning(
                request,
                _(
                    "Your account was created but we couldn't activate your trial. "
                    "Please contact support."
                ),
            )
            return None
        else:
            messages.success(
                request,
                _("Welcome! Your free trial is now active."),
            )
            return settings.LOGIN_REDIRECT_URL


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(
        self,
        request: HttpRequest,
        sociallogin: SocialLogin,
    ) -> bool:
        """
        Determine if social signup is allowed for this request.

        Same logic as AccountAdapter: allow if open registration, has an
        invite token in session, or cloud self-registration is enabled.
        """
        # Always allow signup if user has a workflow invite token
        if request.session.get(WORKFLOW_INVITE_SESSION_KEY):
            return True

        # Always allow signup if user has a trial invite token (cloud)
        if request.session.get(TRIAL_INVITE_SESSION_KEY):
            return True

        # Allow signup if cloud layer has self-registration enabled
        if _is_cloud_self_register():
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

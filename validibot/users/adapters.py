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

# Session key for storing org-level guest invite token during signup
# flow. Set by ``GuestInviteAcceptView`` when an unauthenticated user
# clicks an invite link; consumed by this adapter post-signup to call
# ``invite.accept()`` and reclassify the new user as GUEST.
GUEST_INVITE_SESSION_KEY = "guest_invite_token"

# Session key for storing an org *membership* invite token during signup.
# Set by ``MemberInviteAcceptView`` when an unauthenticated invitee clicks
# the emailed link; consumed by this adapter post-signup to bind the
# invite to the new account and create the Membership. Unlike guest
# invites, member signups are NOT routed through invite-driven
# suppression — a new member keeps the normal personal workspace + BASIC
# classification and *additionally* gains a membership in the inviting org.
MEMBER_INVITE_SESSION_KEY = "member_invite_token"

# Session key for storing cloud trial invite token during signup flow.
# Set by the cloud onboarding AcceptTrialInviteView, consumed after signup
# to activate the trial on the user's personal organization.
TRIAL_INVITE_SESSION_KEY = "trial_invite_token"

# Session key for storing the selected plan during self-register signup.
# Set by the cloud onboarding CloudSignupView from the ?plan=
# querystring on /accounts/signup/, consumed by the email_confirmed
# signal handler to activate the trial with the chosen plan.
SELF_REGISTER_PLAN_SESSION_KEY = "signup_plan"


def _site_invites_enabled() -> bool:
    """True iff guest invites are currently enabled site-wide.

    Module-level helper shared between :class:`AccountAdapter` and
    :class:`SocialAccountAdapter` so both adapters apply the same
    kill-switch logic when validating tokens. Without this, the two
    ``is_open_for_signup`` implementations would have to duplicate
    the SiteSettings lookup and could drift apart.

    Gated on the ``guest_management`` Pro feature: in community
    deployments the kill switch doesn't exist (the whole guest
    invite system is dormant) and this returns True unconditionally,
    matching the convention that all guest-management gating is a
    no-op without Pro.
    """

    from validibot.core.features import CommercialFeature
    from validibot.core.features import is_feature_enabled

    if not is_feature_enabled(CommercialFeature.GUEST_MANAGEMENT):
        return True

    from validibot.core.site_settings import get_site_settings

    return get_site_settings().allow_guest_invites


def _workflow_invite_token_is_redeemable(token: str) -> bool:
    """True iff a workflow invite token is currently redeemable.

    Validates that:

    * The site-wide ``allow_guest_invites`` kill switch is on.
    * The token corresponds to an actual ``WorkflowInvite``.
    * The invite is in PENDING status (not expired, canceled,
      declined, or already accepted).

    Used by both adapters' ``is_open_for_signup`` so a stale token
    cannot open closed registration. One indexed lookup + status
    check + flag read.
    """

    if not _site_invites_enabled():
        return False

    from django.core.exceptions import ValidationError

    from validibot.workflows.models import WorkflowInvite

    try:
        invite = WorkflowInvite.objects.get(token=token)
    except (WorkflowInvite.DoesNotExist, ValueError, ValidationError):
        # ValueError covers some malformed UUIDs; ValidationError
        # covers Django's UUIDField rejecting "not-a-uuid"-style input.
        return False

    invite.mark_expired_if_needed()
    return invite.status == WorkflowInvite.Status.PENDING


def _guest_invite_token_is_redeemable(token: str) -> bool:
    """True iff a guest invite token is currently redeemable.

    Mirrors :func:`_workflow_invite_token_is_redeemable` for the
    org-level ``GuestInvite`` flow.
    """

    if not _site_invites_enabled():
        return False

    from django.core.exceptions import ValidationError

    from validibot.core.constants import InviteStatus
    from validibot.workflows.models import GuestInvite

    try:
        invite = GuestInvite.objects.get(token=token)
    except (GuestInvite.DoesNotExist, ValueError, ValidationError):
        return False

    invite.mark_expired_if_needed()
    return invite.status == InviteStatus.PENDING


def _member_invite_token_is_redeemable(token: str) -> bool:
    """True iff an org-membership invite token is currently redeemable.

    Mirrors :func:`_guest_invite_token_is_redeemable` for the
    ``MemberInvite`` flow. There is no site-wide kill switch for member
    invites (that toggle is guest-specific), so this only verifies the
    token maps to a PENDING invite.

    Used by both adapters' ``is_open_for_signup`` so a brand-new invitee
    can complete signup even on a closed-registration deployment, while a
    stale (expired/canceled/accepted) token cannot reopen it.
    """

    from django.core.exceptions import ValidationError

    from validibot.core.constants import InviteStatus
    from validibot.users.models import MemberInvite

    try:
        invite = MemberInvite.objects.get(token=token)
    except (MemberInvite.DoesNotExist, ValueError, ValidationError):
        return False

    invite.mark_expired_if_needed()
    return invite.status == InviteStatus.PENDING


class AccountAdapter(DefaultAccountAdapter):
    """
    Custom account adapter for Validibot signup flow.

    Handles three types of signups:
    1. Workflow invites: users get access to specific workflows
    2. Trial invites: users from the cloud onboarding flow get a trial org
    3. Self-register: users sign up directly, trial activated from session plan
    """

    def is_open_for_signup(self, request: HttpRequest) -> bool:
        """
        Determine if signup is allowed for this request.

        Signup is allowed if:
        1. The user has a *redeemable* workflow invite token in their session, OR
        2. The user has a *redeemable* guest invite token in their session, OR
        3. The user has a trial invite token in their session (cloud), OR
        4. ``ACCOUNT_ALLOW_REGISTRATION`` is True (open registration).

        Token validation matters here: a stale invite token (expired,
        canceled, kill switch flipped) sitting in session must NOT
        open closed registration. Without validation, an
        ``ACCOUNT_ALLOW_REGISTRATION=False`` deployment could be
        bypassed by sending oneself a guest invite, letting it
        expire, then signing up with the dead token still in session.
        The token is the authorization to sign up; if it's no longer
        valid, signup must be denied.

        Validation runs only when an invite token is the reason
        signup would be allowed — open-registration deployments
        skip the lookup entirely.

        ``ACCOUNT_ALLOW_REGISTRATION=False`` closes self-service signup
        in every deployment, including cloud. The cloud layer used to
        force the gate open whenever ``validibot_cloud.tenancy`` was
        installed; that override silently ignored the operator's
        explicit "close signup" setting and is no longer applied.
        Invite-driven signups are unaffected because they're checked
        before the setting.
        """
        # Workflow invite token: only opens signup if redeemable.
        workflow_token = request.session.get(WORKFLOW_INVITE_SESSION_KEY)
        if workflow_token and _workflow_invite_token_is_redeemable(workflow_token):
            return True

        # Guest invite token: only opens signup if redeemable.
        guest_token = request.session.get(GUEST_INVITE_SESSION_KEY)
        if guest_token and _guest_invite_token_is_redeemable(guest_token):
            return True

        # Member invite token: only opens signup if redeemable. This is
        # the path a brand-new external invitee takes — the emailed link
        # stashes the token, and it must let them sign up even when
        # ``ACCOUNT_ALLOW_REGISTRATION`` is False.
        member_token = request.session.get(MEMBER_INVITE_SESSION_KEY)
        if member_token and _member_invite_token_is_redeemable(member_token):
            return True

        # Always allow signup if user has a trial invite token (cloud)
        if request.session.get(TRIAL_INVITE_SESSION_KEY):
            return True

        return getattr(settings, "ACCOUNT_ALLOW_REGISTRATION", True)

    def save_user(
        self,
        request: HttpRequest,
        user,
        form,
        commit: bool = True,  # noqa: FBT001, FBT002 — must match allauth signature
    ):
        """Save the new user, suppressing default side effects for invite signups.

        When the request session indicates an invite-driven signup
        (workflow or guest invite), wrap the underlying user save in
        :func:`~validibot.users.signals.invite_driven_signup` so the
        post_save signals skip the auto-personal-workspace and auto-
        BASIC classification. The invite-acceptance flow that runs
        afterwards in :meth:`get_signup_redirect_url` handles
        classification + grants explicitly.

        Without this wrapper, a brand-new user invited as a guest
        would land as BASIC with a personal workspace; the GUEST
        classification we apply later wouldn't take because
        ``Membership.clean()`` would reject any subsequent attempts
        to add memberships. Worse, the personal-workspace creation
        itself would conflict with sticky GUEST semantics.
        """

        from validibot.users.signals import invite_driven_signup

        is_invite_signup = bool(
            request.session.get(WORKFLOW_INVITE_SESSION_KEY)
            or request.session.get(GUEST_INVITE_SESSION_KEY),
        )

        if is_invite_signup:
            with invite_driven_signup():
                return super().save_user(request, user, form, commit=commit)
        return super().save_user(request, user, form, commit=commit)

    def pre_login(self, request: HttpRequest, user, **kwargs):
        """Block GUEST-classified users when ``allow_guest_access=False``.

        Hooks into allauth's login flow after credential verification
        but before session establishment. The site-wide
        ``allow_guest_access`` toggle is the operator's incident-
        response kill switch: flip it off and every guest account
        loses access immediately, no migrations required. Existing
        guest rows are kept (the flag is a *gate*, not a destructive
        action), so flipping it back on restores access without data
        loss.

        The check is gated on the ``guest_management`` Pro feature.
        Without Pro the GUEST classification doesn't exist at all and
        the toggle is meaningless; we return early so community
        deployments don't pay the SiteSettings lookup cost on every
        login.

        Returning an ``HttpResponse`` short-circuits allauth's flow;
        returning ``None`` lets login proceed normally. We delegate to
        ``super().pre_login`` first so any upstream check (e.g.
        unverified email, MFA challenge) wins before our gate fires.
        """

        response = super().pre_login(request, user, **kwargs)
        if response is not None:
            return response

        from validibot.core.features import CommercialFeature
        from validibot.core.features import is_feature_enabled

        if not is_feature_enabled(CommercialFeature.GUEST_MANAGEMENT):
            return None

        from validibot.users.constants import UserKindGroup

        if user.user_kind != UserKindGroup.GUEST:
            return None

        from validibot.core.site_settings import get_site_settings

        if get_site_settings().allow_guest_access:
            return None

        # Guest login is disabled. Issue a clear redirect with a flash
        # message so the user understands they aren't blocked due to
        # bad credentials — the credentials *worked*, the kill switch
        # is on.
        from django.contrib import messages
        from django.shortcuts import redirect
        from django.utils.translation import gettext_lazy as _

        messages.error(
            request,
            _(
                "Guest access is currently disabled by the administrator. "
                "Contact support if you believe this is in error.",
            ),
        )
        return redirect("account_login")

    def get_signup_redirect_url(self, request: HttpRequest) -> str:
        """
        Redirect to appropriate destination after signup.

        Checks session keys in priority order:
        1. Workflow invite: accept invite, redirect to workflow launch page
        2. Trial invite: activate trial on user's org, redirect to dashboard
        3. Self-register plan: activate trial with selected plan

        Otherwise, redirect to the default login redirect URL.
        """
        # Track whether we attempted an invite redemption that fell
        # through. If so, we owe the user the default workspace +
        # classification setup that ``save_user`` skipped. Without
        # this, an expired/canceled/missing invite would strand the
        # account with no workspace and no classifier group.
        attempted_invite_redemption = False

        # Check for workflow invite token first
        invite_token = request.session.get(WORKFLOW_INVITE_SESSION_KEY)
        if invite_token:
            attempted_invite_redemption = True
            redirect_url = self._handle_workflow_invite_signup(request, invite_token)
            if redirect_url:
                return redirect_url

        # Check for org-level guest invite token (parallel flow to
        # workflow invites). The new user accepted a GuestInvite link
        # while unauthenticated; ``GuestInviteAcceptView`` stashed the
        # token in session, and now we redeem it.
        guest_invite_token = request.session.get(GUEST_INVITE_SESSION_KEY)
        if guest_invite_token:
            attempted_invite_redemption = True
            redirect_url = self._handle_guest_invite_signup(
                request,
                guest_invite_token,
            )
            if redirect_url:
                return redirect_url

        # Org membership invite (parallel flow). The new user clicked a
        # tokenized member-invite link while unauthenticated;
        # ``MemberInviteAcceptView`` stashed the token and now we redeem
        # it. Deliberately NOT setting ``attempted_invite_redemption``:
        # unlike guest/workflow invites, member signups do not suppress
        # the default side effects in ``save_user``, so the new account
        # already has its personal workspace + BASIC classification. The
        # redemption merely *adds* a membership in the inviting org — so
        # there's no default setup to finalize even if it falls through.
        member_invite_token = request.session.get(MEMBER_INVITE_SESSION_KEY)
        if member_invite_token:
            redirect_url = self._handle_member_invite_signup(
                request,
                member_invite_token,
            )
            if redirect_url:
                return redirect_url

        # Invite redemption was attempted but produced no redirect —
        # i.e. the token was missing/expired/canceled, the operator
        # disabled invites between the click and signup, or
        # ``invite.accept`` raised. The user was created via
        # ``save_user`` with default-side-effect signals suppressed,
        # so they currently have no workspace and no classifier
        # group. Run the default setup now to avoid stranding them.
        if attempted_invite_redemption:
            self._finalize_default_signup(request.user)

        # Trial invite tokens: activation is now handled by the
        # email_confirmed signal in validibot_cloud.onboarding.signals.
        # Just clear the session key if present (it was stashed by
        # AcceptTrialInviteView but is no longer needed here).
        trial_token = request.session.pop(TRIAL_INVITE_SESSION_KEY, None)
        if trial_token:
            logger.debug(
                "Trial invite token cleared from session"
                " (activation deferred to email confirmation)",
            )

        # Self-register plan: activation is now handled by the
        # email_confirmed signal in validibot_cloud.onboarding.signals.
        # Leave the session key in place — the signal handler will
        # consume it when the user confirms their email.
        plan = request.session.get(SELF_REGISTER_PLAN_SESSION_KEY)
        if plan:
            logger.debug(
                "Self-register plan in session"
                " (activation deferred to email confirmation)",
            )

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
        classifying the brand-new user as GUEST (sticky semantics),
        and returns the redirect URL to the workflow launch page.

        Returns None if the invite is invalid (expired, missing, or
        the operator has disabled guest invites), allowing fallback
        to normal signup flow. Whenever this method returns None, the
        caller (``get_signup_redirect_url``) calls
        :meth:`_finalize_default_signup` to provision the personal
        workspace + BASIC classification that ``save_user`` skipped.
        """
        from django.contrib import messages
        from django.utils.translation import gettext_lazy as _

        from validibot.members.views import user_owns_invited_email
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

            # Re-check the operator kill switch. The accept-view ran
            # the same check before stashing the token in session, but
            # the operator may have flipped ``allow_guest_invites`` to
            # False between the anonymous click and signup completion.
            # The kill switch is two-sided by design: pending invites
            # cannot be redeemed during a disable window, even if the
            # token survived in session.
            if not self._invites_enabled():
                logger.info(
                    "Workflow invite %s blocked at signup: "
                    "allow_guest_invites is False",
                    invite_token,
                )
                messages.warning(
                    request,
                    _(
                        "Guest invites are currently disabled by the "
                        "administrator. Your account was created but the "
                        "invite was not redeemed."
                    ),
                )
                return None

            # Email-only invite: only redeem when the new account owns the
            # invited address. A leaked workflow-invite link redeemed during
            # a signup using a *different* email must not grant that account
            # access to a workflow it was never invited to. The brand-new-
            # invitee case redeemed here always has ``invitee_user`` None
            # (existing-account invites are bound by the accept-view), so
            # this is the relevant guard.
            if invite.invitee_user_id is None and not user_owns_invited_email(
                request.user,
                invite.invitee_email,
            ):
                logger.warning(
                    "Workflow invite %s not redeemed: signup account does "
                    "not own the invited email",
                    invite_token,
                )
                messages.warning(
                    request,
                    _(
                        "Your account was created, but this workflow "
                        "invitation is addressed to a different email "
                        "address, so it was not redeemed.",
                    ),
                )
                return None

            # Accept the invite
            grant = invite.accept(user=request.user)

            # Classify the new user as a GUEST. The auto-classify
            # signal was suppressed during ``save_user``, so the user
            # currently has no classifier group; this call is what
            # actually places them in ``Guests``. Without it, the
            # user would land as effectively unclassified — and on
            # the next login, ``user_kind`` would default to BASIC.
            self._classify_invite_signup_as_guest(request.user)

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

    def _handle_guest_invite_signup(
        self,
        request: HttpRequest,
        invite_token: str,
    ) -> str | None:
        """Handle org-level guest invite acceptance after signup.

        Mirrors :meth:`_handle_workflow_invite_signup` but for
        ``GuestInvite`` (org-level): redeems the token, creates the
        appropriate access shape (per-workflow grants for SELECTED
        scope, an ``OrgGuestAccess`` row for ALL scope), and
        classifies the brand-new user as GUEST.

        Returns the URL to redirect to after a successful redemption,
        or None if the invite is invalid (allowing fallback to the
        normal post-signup redirect).
        """

        from django.contrib import messages
        from django.utils.translation import gettext_lazy as _

        from validibot.core.constants import InviteStatus
        from validibot.members.views import user_owns_invited_email
        from validibot.workflows.models import GuestInvite

        del request.session[GUEST_INVITE_SESSION_KEY]

        try:
            invite = GuestInvite.objects.select_related("org").get(
                token=invite_token,
            )

            invite.mark_expired_if_needed()
            if invite.status != InviteStatus.PENDING:
                logger.warning(
                    "Guest invite %s is no longer pending (status: %s)",
                    invite_token,
                    invite.status,
                )
                messages.warning(
                    request,
                    _("The guest invite is no longer valid."),
                )
                return None

            # Re-check the operator kill switch (mirrors the workflow
            # invite handler). The accept-view also runs this check,
            # but the operator may have flipped the flag between the
            # anonymous click and signup completion. Two-sided
            # enforcement means redemption is blocked here too.
            if not self._invites_enabled():
                logger.info(
                    "Guest invite %s blocked at signup: allow_guest_invites is False",
                    invite_token,
                )
                messages.warning(
                    request,
                    _(
                        "Guest invites are currently disabled by the "
                        "administrator. Your account was created but the "
                        "invite was not redeemed."
                    ),
                )
                return None

            # Email-only invite: only redeem when the new account owns the
            # invited address. Without this, a forwarded/leaked guest-invite
            # link redeemed during a signup that uses a *different* email
            # would grant that account guest access to the org it was never
            # invited to. ``invitee_user`` is set only when the invite was
            # minted for an existing account, which the accept-view already
            # binds; the brand-new-invitee case we redeem here always has
            # ``invitee_user`` None, so this is the relevant guard.
            if invite.invitee_user_id is None and not user_owns_invited_email(
                request.user,
                invite.invitee_email,
            ):
                logger.warning(
                    "Guest invite %s not redeemed: signup account does not "
                    "own the invited email",
                    invite_token,
                )
                messages.warning(
                    request,
                    _(
                        "Your account was created, but this guest invitation "
                        "is addressed to a different email address, so it was "
                        "not redeemed.",
                    ),
                )
                return None

            invite.accept(user=request.user)

            # Sticky semantics: the new user's first relationship to
            # Validibot is being a guest of this org, so classify them
            # as GUEST. The auto-classify signal was suppressed during
            # ``save_user`` precisely to leave this decision to the
            # invite-flow code.
            self._classify_invite_signup_as_guest(request.user)

            messages.success(
                request,
                _(
                    "Welcome! You now have guest access to %(org)s.",
                )
                % {"org": invite.org.name},
            )

            # Direct guests to their shared-workflows view, which is
            # the dedicated guest-friendly listing. Other surfaces
            # (workflow detail, dashboard) require an active org
            # membership which guests don't have.
            return reverse("workflows:guest_workflow_list")

        except GuestInvite.DoesNotExist:
            logger.warning("Guest invite not found: %s", invite_token)
            return None
        except ValueError as e:
            logger.warning("Failed to accept guest invite: %s", e)
            messages.error(request, str(e))
            return None

    def _handle_member_invite_signup(
        self,
        request: HttpRequest,
        invite_token: str,
    ) -> str | None:
        """Handle org-membership invite acceptance after signup.

        Redeems a tokenized ``MemberInvite`` on the brand-new account:
        binds the invite to the user, creates the ``Membership`` with the
        invited roles, logs analytics, notifies the inviter, and drops the
        user into the inviting org.

        Returns the post-accept redirect URL, or None if the invite is
        invalid (missing/expired/canceled/already accepted), addressed to
        a different existing user, or the org is at its seat cap — in
        which case the caller falls back to the normal post-signup
        redirect. Nothing is stranded on fallback: ``save_user`` already
        provisioned the personal workspace + BASIC classification for
        member signups (we don't suppress those), so the account is fully
        functional even without the org membership.
        """

        from django.contrib import messages
        from django.shortcuts import resolve_url
        from django.utils.translation import gettext_lazy as _

        from validibot.core.constants import InviteStatus
        from validibot.members.views import finalize_member_invite_accept
        from validibot.members.views import user_owns_invited_email
        from validibot.users.models import MemberInvite
        from validibot.users.seats import SeatQuotaExceededError

        del request.session[MEMBER_INVITE_SESSION_KEY]

        try:
            invite = MemberInvite.objects.select_related("org").get(
                token=invite_token,
            )
        except MemberInvite.DoesNotExist:
            logger.warning("Member invite not found: %s", invite_token)
            return None

        invite.mark_expired_if_needed()
        if invite.status != InviteStatus.PENDING:
            logger.warning(
                "Member invite %s is no longer pending (status: %s)",
                invite_token,
                invite.status,
            )
            messages.warning(request, _("The invitation is no longer valid."))
            return None

        # If the invite named a specific existing user that isn't this new
        # account, don't redeem it on the wrong person. (The email-only
        # invites we actually send have ``invitee_user`` None, so binding
        # proceeds for the expected brand-new-invitee case.)
        if invite.invitee_user_id and invite.invitee_user_id != request.user.id:
            logger.warning(
                "Member invite %s addressed to a different user; not redeeming",
                invite_token,
            )
            return None

        # Email-only invite (``invitee_user`` None): only bind it to this
        # brand-new account if the account provably owns the invited
        # address. A leaked invite link redeemed by a signup using a
        # *different* email must not silently grant membership addressed to
        # someone else. Mirrors the guard in ``MemberInviteAcceptView`` so
        # the anonymous->signup path is no weaker than the logged-in click.
        if invite.invitee_user_id is None and not user_owns_invited_email(
            request.user,
            invite.invitee_email,
        ):
            logger.warning(
                "Member invite %s not redeemed: signup account does not own "
                "the invited email",
                invite_token,
            )
            messages.warning(
                request,
                _(
                    "Your account was created, but this invitation is "
                    "addressed to a different email address, so it was not "
                    "redeemed.",
                ),
            )
            return None

        try:
            finalize_member_invite_accept(invite, request.user)
        except SeatQuotaExceededError as exc:
            logger.info("Member invite %s blocked by seat cap", invite_token)
            messages.error(request, str(exc))
            return None

        messages.success(
            request,
            _("Welcome! You're now a member of %(org)s.") % {"org": invite.org.name},
        )
        request.user.set_current_org(invite.org)
        # ``LOGIN_REDIRECT_URL`` is a URL name; resolve_url handles both
        # names and paths.
        return resolve_url(settings.LOGIN_REDIRECT_URL)

    def _invites_enabled(self) -> bool:
        """Return True iff guest invites are currently enabled site-wide.

        Thin wrapper over the module-level
        :func:`_site_invites_enabled` so existing call sites
        (``_handle_*_invite_signup``) don't need to be rewritten and
        future logic that wants to read the same flag from instance
        context still has a method to lean on.
        """

        return _site_invites_enabled()

    def _finalize_default_signup(self, user) -> None:
        """Provision the default workspace + classification for a new user.

        Called from ``get_signup_redirect_url`` whenever an invite
        redemption flow returned None — the user was created via
        ``save_user`` with both default-side-effect signals
        suppressed (``invite_driven_signup`` ContextVar), so without
        this fallback they'd land stranded with no workspace and no
        classifier group.

        Idempotent: if the user already has a personal workspace or
        a classifier group (e.g. signals fired anyway because the
        ContextVar wasn't actually active), the helpers are no-ops.
        """

        from validibot.users.models import ensure_personal_workspace

        ensure_personal_workspace(user)

        # Classification is Pro-only; the helper checks the feature
        # flag and skips cleanly in community.
        from validibot.core.features import CommercialFeature
        from validibot.core.features import is_feature_enabled

        if not is_feature_enabled(CommercialFeature.GUEST_MANAGEMENT):
            return

        from validibot.users.constants import UserKindGroup
        from validibot.users.user_kind import classify_as_basic

        # Don't reclassify if the user already has a kind (e.g. they
        # were placed in Guests by an earlier code path that we've
        # since fallen back from). Default for an unclassified user
        # is BASIC.
        if user.user_kind != UserKindGroup.GUEST:
            classify_as_basic(user)

    def _classify_invite_signup_as_guest(self, user) -> None:
        """Classify a brand-new invite-driven user as GUEST.

        The classification is gated on the ``guest_management`` Pro
        feature: in community deployments the GUEST kind doesn't
        exist, so calling ``classify_as_guest`` would create an
        unused ``Guests`` group entry. Skip cleanly in that case.
        """

        from validibot.core.features import CommercialFeature
        from validibot.core.features import is_feature_enabled

        if not is_feature_enabled(CommercialFeature.GUEST_MANAGEMENT):
            return

        from validibot.users.user_kind import classify_as_guest

        classify_as_guest(user)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(
        self,
        request: HttpRequest,
        sociallogin: SocialLogin,
    ) -> bool:
        """
        Determine if social signup is allowed for this request.

        Same logic as :meth:`AccountAdapter.is_open_for_signup` — must
        stay in lockstep with the password-signup branch so a tokenized
        guest invite redeems via either signup mechanism. Without the
        guest invite branch, an operator running closed registration
        (``ACCOUNT_ALLOW_REGISTRATION=False``) would accept guest
        invites for password signups but reject the same invites for
        social signups.
        """
        # Workflow / guest invite tokens only open signup if currently
        # redeemable. Validating here mirrors the password adapter and
        # stops a stale (expired/canceled/kill-switched) token from
        # bypassing closed registration just because it's still in
        # session from an earlier click.
        workflow_token = request.session.get(WORKFLOW_INVITE_SESSION_KEY)
        if workflow_token and _workflow_invite_token_is_redeemable(workflow_token):
            return True

        guest_token = request.session.get(GUEST_INVITE_SESSION_KEY)
        if guest_token and _guest_invite_token_is_redeemable(guest_token):
            return True

        member_token = request.session.get(MEMBER_INVITE_SESSION_KEY)
        if member_token and _member_invite_token_is_redeemable(member_token):
            return True

        # Always allow signup if user has a trial invite token (cloud)
        if request.session.get(TRIAL_INVITE_SESSION_KEY):
            return True

        return getattr(settings, "ACCOUNT_ALLOW_REGISTRATION", True)

    def save_user(
        self,
        request: HttpRequest,
        sociallogin: SocialLogin,
        form=None,
    ):
        """Wrap social signup in invite-driven suppression when applicable.

        Mirrors :meth:`AccountAdapter.save_user` for the social flow.
        Without this wrapper, a brand-new user invited as a guest who
        then signs up with Google/GitHub/etc. would have the post_save
        signals run normally — landing as BASIC with a personal
        workspace, conflicting with the GUEST classification the
        invite-flow code applies afterwards via
        :meth:`AccountAdapter.get_signup_redirect_url` (which allauth
        invokes for both password and social signups).
        """

        from validibot.users.signals import invite_driven_signup

        is_invite_signup = bool(
            request.session.get(WORKFLOW_INVITE_SESSION_KEY)
            or request.session.get(GUEST_INVITE_SESSION_KEY),
        )

        if is_invite_signup:
            with invite_driven_signup():
                return super().save_user(request, sociallogin, form=form)
        return super().save_user(request, sociallogin, form=form)

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

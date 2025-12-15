# ADR-2025-11-28: Invite-Based Signup and Organization Seat Management

**Status:** Proposed (2025-11-28)  
**Owners:** Platform / Users  
**Related ADRs:** [ADR-2025-11-28: Pricing System](2025-11-28-pricing-system.md) (seats/limits), [ADR: Invite-Only Free Access and Cross-Org Workflow Sharing](2025-12-15-free-tier-and-workflow-sharing.md) (workflow guests/sharing)  
**Related docs:** `dev_docs/data-model/users_roles.md`, `dev_docs/organization_management.md`

---

> Note: This ADR covers inviting users into an organization (seat-based membership). For workflow-level sharing with external users that do not consume seats (Workflow Guests), see [ADR: Invite-Only Free Access and Cross-Org Workflow Sharing](2025-12-15-free-tier-and-workflow-sharing.md).

## Context

### The Problem

Validibot currently supports two ways for users to join an organization:

1. **Self-signup** – A new user creates an account and gets an auto-provisioned personal workspace.
2. **Invite existing user** – An admin invites an existing Validibot user to their organization via `PendingInvite`. The invitee sees a notification and can accept/decline.

However, there's a gap: **we cannot invite someone who doesn't yet have a Validibot account**. This is a common B2B scenario:

> "I want to invite my colleague alice@acme.com to my organization. She doesn't have an account yet—she should sign up and land directly in my org."

This introduces several related requirements:

1. **Invite-based signup flow** – New users invited by email should see a custom signup page tied to the invite.
2. **Seat-based billing** – Organizations have plans with seat limits. Inviting a user consumes a seat.
3. **Users without personal workspaces** – An invited user may only belong to the inviting org, with no subscription of their own.

### Industry Patterns

The standard B2B SaaS pattern (used by Slack, GitHub, Notion, Linear, Figma, etc.) is:

| Entity                | Responsibility                                           |
| --------------------- | -------------------------------------------------------- |
| **User**              | Identity and authentication only—no billing relationship |
| **Organization**      | Billing entity, owns the subscription, has seat limits   |
| **Membership**        | Links user to org, grants roles, **consumes a seat**     |
| **Subscription/Plan** | Defines seat count, feature limits, billing cycle        |

In this model:

- Users don't "have" plans—Organizations do.
- An invited user is just a `User` + `Membership`; they consume a seat in the org's plan.
- Users can belong to multiple orgs (each with its own subscription).
- If a user leaves all orgs, they have no access—but they can create their own org (and subscribe) if desired.

### What We Already Have

Our current model is well-aligned with this pattern:

```
User (identity/auth only)
  │
  └─► Membership ──► Organization
        │                │
        └─► roles        └─► OrgQuota (usage limits)
```

Key existing components:

- **`User`** – Pure identity, no billing fields.
- **`Organization`** – Tenant boundary with `is_personal` flag.
- **`Membership`** – Links user to org with `is_active` flag and roles.
- **`PendingInvite`** – Stores invite with `invitee_email`, `roles`, `token`, `status`, `expires_at`.
- **`OrgQuota`** – Per-org limits (`max_submissions_per_day`, `max_run_minutes_per_day`, `artifact_retention_days`).

What's missing:

1. **Seat limit on `OrgQuota`** (or a separate `Subscription` model).
2. **Signup flow for email-only invites** (currently `PendingInvite.invitee_email` exists but has no signup UI).
3. **Seat enforcement** on invite creation and membership activation.
4. **Decision on personal workspaces** for invited users.

---

## Decision

> Note: This ADR depends on the Pricing System ADR to define how seats are sold and metered. Seat limits and billing enforcement should follow the plan definitions there. Implement pricing first, then wire the seat model here.

We will implement invite-based signup with seat management as follows:

### 1. Subscription and Seat Tracking

Add seat tracking to `OrgQuota` (MVP) with a path to a dedicated `Subscription` model later:

```python
# validibot/billing/models.py

class OrgQuota(TimeStampedModel):
    """
    Per-organization usage limits and subscription entitlements.

    For MVP, seat limits live here alongside usage quotas. When we add Stripe
    integration, we may split subscription details into a dedicated model.
    """

    org = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="quota",
    )

    # Existing usage limits
    max_submissions_per_day = models.IntegerField(default=100)
    max_run_minutes_per_day = models.IntegerField(default=60)
    artifact_retention_days = models.IntegerField(default=7)

    # NEW: Seat management
    max_seats = models.IntegerField(
        default=5,
        help_text=_("Maximum number of active members allowed in this organization."),
    )

    def used_seats(self) -> int:
        """Count active memberships (each consumes one seat)."""
        return self.org.memberships.filter(is_active=True).count()

    def available_seats(self) -> int:
        """Seats remaining before hitting the limit."""
        return max(0, self.max_seats - self.used_seats())

    def has_available_seats(self, count: int = 1) -> bool:
        """Check if the org can add `count` more members."""
        return self.available_seats() >= count
```

**Constants for plan tiers** (in `billing/constants.py`):

```python
class PlanTier(models.TextChoices):
    """
    Subscription plan tiers. Seat limits and features vary by tier.
    """

    FREE = "FREE", _("Free")
    STARTER = "STARTER", _("Starter")
    GROWTH = "GROWTH", _("Growth")
    ENTERPRISE = "ENTERPRISE", _("Enterprise")


# Default seat limits per tier (can be overridden per-org)
DEFAULT_SEATS_BY_TIER = {
    PlanTier.FREE: 3,
    PlanTier.STARTER: 5,
    PlanTier.GROWTH: 15,
    PlanTier.ENTERPRISE: 100,  # Typically custom
}
```

### 2. Seat Enforcement

Enforce seat limits at two points:

#### 2.1 When Creating an Invite

```python
# validibot/members/services.py

from django.core.exceptions import ValidationError
from validibot.billing.constants import SeatLimitError

class InviteService:
    """
    Service for creating and managing organization invites.
    """

    def create_invite(
        self,
        org: Organization,
        inviter: User,
        invitee_email: str,
        roles: list[str],
        invitee_user: User | None = None,
    ) -> PendingInvite:
        """
        Create a new invite, enforcing seat limits.

        Args:
            org: The organization to invite into.
            inviter: The user sending the invite.
            invitee_email: Email address of the invitee.
            roles: Role codes to grant on acceptance.
            invitee_user: Optional existing user (if known).

        Raises:
            SeatLimitError: If the org has no available seats.
            ValidationError: If a pending invite already exists for this email.
        """
        # Check seat availability
        quota = getattr(org, "quota", None)
        if quota and not quota.has_available_seats():
            raise SeatLimitError(
                detail=_(
                    "This organization has reached its seat limit (%(used)s/%(max)s). "
                    "Upgrade your plan or remove inactive members to invite more users."
                ) % {"used": quota.used_seats(), "max": quota.max_seats},
                code="seat_limit_reached",
            )

        # Check for existing pending invite
        existing = PendingInvite.objects.filter(
            org=org,
            invitee_email__iexact=invitee_email,
            status=PendingInvite.Status.PENDING,
        ).exists()
        if existing:
            raise ValidationError(
                _("A pending invite already exists for this email address."),
            )

        return PendingInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee_email,
            invitee_user=invitee_user,
            roles=roles,
        )
```

#### 2.2 When Accepting an Invite (Double-Check)

The `PendingInvite.accept()` method should re-check seat availability in case the org filled up while the invite was pending:

```python
# In PendingInvite.accept()

def accept(self, user: User | None = None, roles: list[str] | None = None) -> Membership:
    """
    Accept the invite and create a membership.

    Re-checks seat availability to handle race conditions where the org
    filled up while this invite was pending.
    """
    self.mark_expired_if_needed()
    if self.status != self.Status.PENDING:
        raise ValueError("Invite is not pending.")

    # Bind user if provided (for email-only invites where user just signed up)
    if user and self.invitee_user is None:
        self.invitee_user = user
        self.save(update_fields=["invitee_user"])

    if self.invitee_user is None:
        raise ValueError("Invitee user is not set; cannot accept without user.")

    # Check if user is already a member (reactivation case)
    existing_membership = Membership.objects.filter(
        user=self.invitee_user,
        org=self.org,
    ).first()

    if existing_membership and existing_membership.is_active:
        # Already an active member—just update roles
        membership_roles = roles or self.roles or []
        existing_membership.set_roles(set(membership_roles))
        self.status = self.Status.ACCEPTED
        self.save(update_fields=["status"])
        return existing_membership

    # Re-check seat availability (may have filled while invite was pending)
    quota = getattr(self.org, "quota", None)
    if quota and not quota.has_available_seats():
        raise SeatLimitError(
            detail=_(
                "This organization has reached its seat limit. "
                "Please contact the organization admin."
            ),
            code="seat_limit_reached",
        )

    # Create or reactivate membership
    membership_roles = roles or self.roles or []
    membership, created = Membership.objects.get_or_create(
        user=self.invitee_user,
        org=self.org,
        defaults={"is_active": True},
    )
    if not created:
        membership.is_active = True
        membership.save(update_fields=["is_active"])

    membership.set_roles(set(membership_roles))
    self.status = self.Status.ACCEPTED
    self.save(update_fields=["status"])
    return membership
```

### 3. Invite-Based Signup Flow

#### 3.1 URL Structure

```python
# validibot/users/urls.py (or accounts/urls.py)

urlpatterns = [
    # Standard signup (creates personal workspace)
    path("signup/", views.SignupView.as_view(), name="account_signup"),

    # Invite-based signup (joins specific org, no personal workspace)
    path(
        "signup/invite/<uuid:token>/",
        views.InviteSignupView.as_view(),
        name="account_signup_invite",
    ),
]
```

#### 3.2 View Implementation

```python
# validibot/users/views.py

from django.views.generic import FormView
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from django.contrib.auth import login

class InviteSignupView(FormView):
    """
    Signup page for users who were invited by email.

    Displays the inviting organization's name and restricts the email field
    to the invited address. On successful signup, the user is added to the
    organization with the invited roles (no personal workspace is created).
    """

    template_name = "account/signup_invite.html"
    form_class = InviteSignupForm

    def dispatch(self, request, *args, **kwargs):
        # Load and validate the invite
        self.invite = get_object_or_404(
            PendingInvite.objects.select_related("org", "inviter"),
            token=kwargs["token"],
        )
        self.invite.mark_expired_if_needed()

        if self.invite.status != PendingInvite.Status.PENDING:
            messages.error(request, _("This invitation is no longer valid."))
            return redirect("account_login")

        # If invitee already has an account, redirect to login
        if self.invite.invitee_user is not None:
            messages.info(
                request,
                _("You already have an account. Please log in to accept this invite."),
            )
            return redirect("account_login")

        # Check if email is already registered
        existing_user = User.objects.filter(
            email__iexact=self.invite.invitee_email,
        ).first()
        if existing_user:
            # Bind invite to existing user and redirect to login
            self.invite.invitee_user = existing_user
            self.invite.save(update_fields=["invitee_user"])
            messages.info(
                request,
                _("An account exists for this email. Please log in to accept the invite."),
            )
            return redirect("account_login")

        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["invite"] = self.invite
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "invite": self.invite,
            "org": self.invite.org,
            "inviter": self.invite.inviter,
        })
        return context

    def form_valid(self, form):
        # Create the user
        user = form.save()

        # Bind invite to user and accept it
        self.invite.invitee_user = user
        self.invite.save(update_fields=["invitee_user"])

        try:
            self.invite.accept()
        except SeatLimitError as e:
            messages.error(self.request, str(e.detail))
            return redirect("account_login")

        # Set the joined org as current (no personal workspace)
        user.set_current_org(self.invite.org)

        # Log the user in
        login(self.request, user)

        messages.success(
            self.request,
            _("Welcome to %(org)s! Your account has been created.") % {
                "org": self.invite.org.name,
            },
        )
        return redirect("workflows:workflow_list")
```

#### 3.3 Form Implementation

```python
# validibot/users/forms.py

class InviteSignupForm(forms.Form):
    """
    Signup form for invite-based registration.

    The email field is pre-filled and read-only (locked to the invited address).
    """

    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"readonly": "readonly"}),
        help_text=_("This is the email address you were invited with."),
    )
    name = forms.CharField(
        max_length=255,
        required=False,
        help_text=_("Your display name (optional)."),
    )
    password1 = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput,
        help_text=_("Choose a secure password."),
    )
    password2 = forms.CharField(
        label=_("Confirm password"),
        widget=forms.PasswordInput,
    )

    def __init__(self, *args, invite: PendingInvite = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.invite = invite
        if invite:
            self.fields["email"].initial = invite.invitee_email

    def clean_email(self):
        """Ensure email matches the invite (prevent tampering)."""
        email = self.cleaned_data["email"]
        if self.invite and email.lower() != self.invite.invitee_email.lower():
            raise ValidationError(
                _("Email must match the invited address."),
            )
        return email

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise ValidationError({"password2": _("Passwords do not match.")})
        return cleaned_data

    def save(self) -> User:
        """Create the user account."""
        email = self.cleaned_data["email"]
        password = self.cleaned_data["password1"]
        name = self.cleaned_data.get("name", "")

        user = User.objects.create_user(
            username=email,  # or generate from email
            email=email,
            password=password,
            name=name,
        )
        return user
```

#### 3.4 Template

```django
{# templates/account/signup_invite.html #}
{% extends "account/base_entrance.html" %}
{% load i18n crispy_forms_tags %}

{% block title %}{% trans "Join" %} {{ org.name }}{% endblock %}

{% block content %}
<div class="card">
  <div class="card-header">
    <h4>{% trans "You've been invited!" %}</h4>
  </div>
  <div class="card-body">
    <p class="lead">
      <strong>{{ inviter.name|default:inviter.email }}</strong>
      {% trans "has invited you to join" %}
      <strong>{{ org.name }}</strong>
      {% trans "on Validibot." %}
    </p>

    <hr>

    <p>{% trans "Create your account to get started:" %}</p>

    <form method="post" novalidate>
      {% csrf_token %}
      {{ form|crispy }}

      <div class="d-grid gap-2 mt-4">
        <button type="submit" class="btn btn-primary btn-lg">
          {% trans "Create account and join" %} {{ org.name }}
        </button>
      </div>
    </form>

    <p class="text-muted mt-3 small">
      {% blocktrans %}
      By signing up, you agree to our Terms of Service and Privacy Policy.
      {% endblocktrans %}
    </p>
  </div>
</div>
{% endblock %}
```

### 4. Personal Workspace Policy for Invited Users

We adopt **Option C: Lazy Personal Workspace**:

- **Invite-based signup**: User joins only the inviting org. No personal workspace is created.
- **Self-signup**: User gets a personal workspace (existing behavior).
- **Later**: If an invited user wants their own workspace, they can create one from the org switcher UI.

This requires a small change to `ensure_personal_workspace()`:

```python
def ensure_personal_workspace(user: "User", *, force: bool = False) -> "Organization | None":
    """
    Ensure a personal workspace exists for the user.

    Args:
        user: The user to create a workspace for.
        force: If True, create workspace even if user has other orgs.

    Returns:
        The personal workspace Organization, or None if skipped.
    """
    # Check for existing personal workspace
    existing = (
        user.orgs.filter(is_personal=True, membership__is_active=True)
        .distinct()
        .first()
    )
    if existing:
        ensure_default_project(existing)
        if not user.current_org_id:
            user.set_current_org(existing)
        return existing

    # Skip if user already has an org (invited user) and not forcing
    if not force and user.memberships.filter(is_active=True).exists():
        return None

    # Create personal workspace (existing logic)
    name = _workspace_name_for(user)
    slug = _generate_unique_slug(Organization, name, prefix="workspace-")
    personal_org = Organization.objects.create(
        name=name,
        slug=slug,
        is_personal=True,
    )
    membership = Membership.objects.create(
        user=user,
        org=personal_org,
        is_active=True,
    )
    membership.set_roles({RoleCode.ADMIN, RoleCode.OWNER, RoleCode.EXECUTOR})
    ensure_default_project(personal_org)
    user.set_current_org(personal_org)
    return personal_org
```

### 5. Invite Email with Signup Link

When sending an invite to a non-existent user, include the signup link:

```python
# validibot/members/services.py

def send_invite_email(invite: PendingInvite) -> None:
    """
    Send an invitation email to the invitee.

    If the invitee has an account, link to the notifications page.
    If not, link to the invite-specific signup page.
    """
    if invite.invitee_user:
        # Existing user—they'll see it in notifications
        action_url = reverse("notifications:notification-list")
        template = "members/email/invite_existing_user.html"
    else:
        # New user—send signup link
        action_url = reverse(
            "account_signup_invite",
            kwargs={"token": invite.token},
        )
        template = "members/email/invite_new_user.html"

    context = {
        "invite": invite,
        "org": invite.org,
        "inviter": invite.inviter,
        "action_url": action_url,
        "roles": invite.roles,
        "expires_at": invite.expires_at,
    }

    send_mail(
        subject=_("You've been invited to join %(org)s on Validibot") % {
            "org": invite.org.name,
        },
        message=render_to_string(f"{template}.txt", context),
        html_message=render_to_string(template, context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[invite.invitee_email],
    )
```

### 6. Error Handling and User Feedback

Define a custom exception for seat limits:

```python
# validibot/billing/constants.py

class SeatLimitError(Exception):
    """
    Raised when an organization has reached its seat limit.
    """

    def __init__(self, detail: str, code: str = "seat_limit_reached"):
        self.detail = detail
        self.code = code
        super().__init__(detail)
```

UI messaging:

| Scenario                  | Message                                                                                                                  |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Invite creation blocked   | "This organization has reached its seat limit (X/Y). Upgrade your plan or remove inactive members to invite more users." |
| Invite acceptance blocked | "This organization has reached its seat limit. Please contact the organization admin."                                   |
| Successful invite signup  | "Welcome to [Org Name]! Your account has been created."                                                                  |
| Expired invite            | "This invitation has expired. Please ask [Inviter] to send a new one."                                                   |
| Already a member          | "You're already a member of [Org Name]."                                                                                 |

### 7. Seat Management UI

Add a seats indicator to the org admin UI:

```django
{# templates/members/member_list.html (partial) #}

<div class="card-header d-flex justify-content-between align-items-center">
  <h5>{% trans "Members" %}</h5>
  <div>
    {% if org.quota %}
      <span class="badge bg-secondary me-2">
        {{ org.quota.used_seats }}/{{ org.quota.max_seats }} {% trans "seats used" %}
      </span>
    {% endif %}
    {% if can_invite %}
      <a href="{% url 'members:invite' %}" class="btn btn-primary btn-sm">
        {% trans "Invite member" %}
      </a>
    {% endif %}
  </div>
</div>

{% if org.quota and org.quota.available_seats == 0 %}
  <div class="alert alert-warning m-3">
    {% blocktrans %}
    Your organization has reached its seat limit. To invite more members,
    upgrade your plan or remove inactive members.
    {% endblocktrans %}
    <a href="{% url 'billing:plans' %}">{% trans "View plans" %}</a>
  </div>
{% endif %}
```

---

## Data Model Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                            User                                  │
│  (identity only—no billing relationship)                        │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       │ Membership (consumes 1 seat)
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Organization                              │
│  name, slug, is_personal                                         │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       │ OneToOne
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                         OrgQuota                                 │
│  max_seats, max_submissions_per_day, max_run_minutes_per_day,   │
│  artifact_retention_days                                         │
│                                                                  │
│  + used_seats() → count of active Memberships                    │
│  + available_seats() → max_seats - used_seats                    │
│  + has_available_seats(n) → available_seats >= n                 │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                       PendingInvite                              │
│  org, inviter, invitee_user (nullable), invitee_email,          │
│  roles, status, token, expires_at                                │
│                                                                  │
│  + accept(user) → creates Membership, checks seats               │
│  + decline()                                                     │
│  + mark_expired_if_needed()                                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## MVP Scope

### In Scope (January Alpha)

1. **`max_seats` field on `OrgQuota`** with default of 5.
2. **Seat enforcement** when creating invites and accepting them.
3. **Invite signup page** (`/signup/invite/<token>/`) with locked email field.
4. **Lazy personal workspace** – skip for invited users.
5. **Invite email** with signup link for new users.
6. **Seats indicator** in member management UI.
7. **Upgrade prompt** when seats are exhausted.
8. **Basic tests** for seat enforcement and invite signup flow.

### Out of Scope (Future)

- Stripe subscription integration (payment for seat upgrades).
- Dedicated `Subscription` model (using `OrgQuota` for MVP).
- Seat overage handling (what happens if plan downgrades?).
- Invite link sharing (currently email-based only).
- SSO/SAML invite flow.
- Domain-based auto-join (e.g., anyone with @acme.com can join Acme's org).
- Seat reservations (holding seats for pending invites).

---

## Security Considerations

1. **Token secrecy** – Invite tokens are UUIDs and should be treated as secrets. They are single-use (status transitions from PENDING) and expire after 7 days.

2. **Email verification** – We don't require email verification before accepting an invite because the invite itself proves the email was reachable. However, we lock the signup form to the invited email to prevent account hijacking.

3. **Seat race conditions** – We check seats both at invite creation and acceptance. This handles the case where an org fills up while an invite is pending.

4. **Invite enumeration** – The `/signup/invite/<token>/` endpoint returns a 404 for invalid tokens to prevent enumeration.

5. **Spam prevention** – Admins can only invite as many users as they have seats. Rate limiting on invite creation may be added later.

---

## Consequences

### Positive

1. **Complete invite flow** – Admins can now invite anyone, not just existing users.
2. **Seat-based billing foundation** – Ready for Stripe integration when needed.
3. **Industry-standard UX** – Matches patterns users expect from Slack, Notion, etc.
4. **Clean separation** – Users remain identity-only; billing stays on Organization.
5. **Flexible membership** – Users can belong to multiple orgs naturally.

### Negative

1. **Complexity** – More code paths (self-signup vs. invite-signup).
2. **Orphan users** – Users who lose all memberships have an empty experience (need "create org" flow).
3. **Seat management burden** – Admins must manage seats (though this is expected for B2B SaaS).

### Neutral

1. **Migration** – Existing orgs get default `max_seats=5` (or higher for existing member counts).
2. **allauth integration** – May need to customize allauth signup flow or bypass it for invite signups.

---

## Implementation Checklist

### Models & Migrations

- [ ] Add `max_seats` to `OrgQuota` model
- [ ] Add `used_seats()`, `available_seats()`, `has_available_seats()` methods
- [ ] Add `PlanTier` constant and `DEFAULT_SEATS_BY_TIER` mapping
- [ ] Create `SeatLimitError` exception class
- [ ] Migration to add `max_seats` with sensible default

### Invite Flow

- [ ] Update `PendingInvite.accept()` to check seats and accept `user` param
- [ ] Create `InviteService.create_invite()` with seat checking
- [ ] Create `InviteSignupView` and `InviteSignupForm`
- [ ] Create `signup_invite.html` template
- [ ] Add URL route for `/signup/invite/<token>/`
- [ ] Update `send_invite_email()` to use signup link for new users

### Personal Workspace

- [ ] Update `ensure_personal_workspace()` to skip for invited users
- [ ] Add "Create workspace" option to org switcher (future)

### UI

- [ ] Add seats indicator to member list page
- [ ] Add upgrade prompt when seats exhausted
- [ ] Add seat count to invite modal validation

### Tests

- [ ] Test seat limit blocks invite creation
- [ ] Test seat limit blocks invite acceptance
- [ ] Test invite signup creates user and membership
- [ ] Test invite signup skips personal workspace
- [ ] Test expired invite shows error
- [ ] Test email mismatch prevented on signup form

### Documentation

- [ ] Update `dev_docs/data-model/users_roles.md` with seat info
- [ ] Update `dev_docs/organization_management.md` with invite flow
- [ ] Add user-facing docs for inviting members

---

## References

- [Stripe Team Billing](https://stripe.com/docs/billing/subscriptions/per-seat) – Per-seat subscription pattern
- [Slack Enterprise Grid](https://slack.com/help/articles/115001435788-Add-members-to-your-Slack-workspace) – Invite flow reference
- [GitHub Organization Billing](https://docs.github.com/en/billing/managing-billing-for-your-github-account/about-per-user-pricing) – Per-user pricing model
- [Notion Team Plans](https://www.notion.so/help/add-members-to-your-workspace) – Workspace invite patterns

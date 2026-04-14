# Configuring Multi-Factor Authentication

Validibot ships with opt-in multi-factor authentication (MFA) powered by
[django-allauth's MFA module](https://docs.allauth.org/en/latest/mfa/).
Authenticator apps (TOTP) and recovery codes are enabled out of the box; any
user can turn MFA on from their **Security** settings page and turn it off
again at any time. MFA is never forced — policies that require it belong in
the Pro/Enterprise tiers.

## Required environment variable: `DJANGO_MFA_ENCRYPTION_KEY`

MFA secret material — TOTP shared secrets and recovery-code seed values —
is stored **encrypted** in the database via a Fernet cipher (AES-128-CBC +
HMAC-SHA256) provided by our custom
[`ValidibotMFAAdapter`](../../../validibot/users/mfa_adapter.py). The
application **refuses to start** in any environment without a valid
encryption key.

### Why a dedicated key (not `SECRET_KEY`)

Django's `SECRET_KEY` rotates on a different schedule. Rotating it
invalidates sessions and signed cookies, which is an acceptable cost for
session hygiene — but it must NOT invalidate every user's long-lived
second factor. A separate `DJANGO_MFA_ENCRYPTION_KEY` lets the two
rotate independently.

### Generate a key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The output is a 44-character URL-safe base64 string ending in `=`
(example: `qYy0eDvn7TRiLVXGJk1XeXgvr1SYathyVc9X-7HIV5E=`). Fernet rejects
anything else — hand-typed strings, random UUIDs, raw bytes — so you
must use the generator above.

### Where to set it

| Environment | File | Notes |
|---|---|---|
| Local dev | `.envs/.local/.django` | Generate once and reuse; never commit |
| GCP production | `.envs/.production/.google-cloud/.django` | Uploaded to Secret Manager via `just gcp secrets prod` |
| Docker Compose production | `.envs/.production/.docker-compose/.django` | Deployed to the target host; keep out of git |
| AWS production | `.envs/.production/.aws/.django` | Store in AWS Secrets Manager or Parameter Store |
| Tests | `config/settings/test.py` (hardcoded) | Fixed test key; never reuse outside tests |

**Never reuse a key across environments.** If dev leaks, prod should be
unaffected. Matching templates in `.envs.example/` document the expected
placement.

### Rotate the key

If the key is compromised (or on a regular schedule), switch to a
`cryptography.fernet.MultiFernet` in
[`validibot/users/mfa_adapter.py`](../../../validibot/users/mfa_adapter.py)
with the new key first and the old key second. Fernet will decrypt with
whichever key works and re-encrypt under the new one on next write. The
current adapter uses a single-key `Fernet`; wire up `MultiFernet` when
rotation is actually needed.

## Other settings

Three additional settings in `config/settings/base.py` drive the feature:

```python
MFA_ADAPTER = "validibot.users.mfa_adapter.ValidibotMFAAdapter"
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_TOTP_ISSUER = "Validibot"
MFA_RECOVERY_CODE_COUNT = 10
```

- **`MFA_ADAPTER`** points allauth at our Fernet-backed adapter. Leaving
  it unset reverts to allauth's default, which stores secrets in
  cleartext — don't.
- **`MFA_SUPPORTED_TYPES`** is the list of second factors allauth will accept.
  TOTP means six-digit codes from apps like Aegis, 1Password, Bitwarden, or
  Google Authenticator. Recovery codes are single-use backup codes a user
  prints or stores in a password manager for the day they lose their phone.
- **`MFA_TOTP_ISSUER`** is the label authenticator apps show next to the
  account email. Without it, users with multiple TOTP entries see bare email
  addresses and have to guess which one is Validibot.
- **`MFA_RECOVERY_CODE_COUNT`** is allauth's default (10). We state it
  explicitly so future maintainers don't have to chase an upstream default
  if we ever audit it.

## Required infrastructure: a shared cache

Allauth's rate limiting and short-window "same TOTP code can't be reused"
checks store state in Django's cache. That cache MUST be shared across
Gunicorn workers and scaled instances — a per-process `LocMemCache` would
silently weaken those protections. `config/settings/production.py`
enforces this by picking between two supported shared backends and
explicitly refusing to fall back to per-process storage.

### Default: `DatabaseCache` on the existing Postgres database

Reuses the Cloud SQL / Postgres instance you already have, so there's no
new infrastructure and no incremental cost. Fine for the low-volume
rate-limit workload (a few hundred cache ops/day) that this app sees at
pre-release scale.

One one-time setup step per environment:

```bash
python manage.py createcachetable
```

This provisions the `django_cache` table. Skip it on subsequent deploys
— the table persists across code changes.

### Upgrade path: Redis via Memorystore

When traffic grows enough that DB-backed cache latency starts showing up
in auth-path monitoring, or when you want separation between cache and
primary data, switch to Redis:

1. Provision Memorystore for Redis (smallest BASIC-tier instance on GCP
   ≈ $35/month as of writing).
2. Attach Cloud Run to the VPC connector that can reach Memorystore.
3. Set `REDIS_URL=redis://host:port/db` in the production env file and
   `just gcp secrets prod`.
4. Redeploy. Nothing else changes — `config/settings/production.py`
   auto-switches to `RedisCache` when `REDIS_URL` is set.

The DB-backed cache keeps working if you need to roll back: remove
`REDIS_URL` and redeploy.

### Other deployment targets

- **Docker Compose**: bundled `redis` service covers it; `REDIS_URL` is
  already wired in the compose file.
- **AWS**: use ElastiCache; same `REDIS_URL` setting.

## Adding WebAuthn / passkeys later

Enabling WebAuthn is two pieces of work:

1. **Append `"webauthn"` to `MFA_SUPPORTED_TYPES`.** No migration is required,
   because allauth stores authenticators in a single polymorphic table keyed
   by `type`.

   ```python
   MFA_SUPPORTED_TYPES = ["totp", "recovery_codes", "webauthn"]
   ```

2. **Write per-page template overrides** for each allauth WebAuthn management
   page. At time of writing those are `mfa/webauthn/authenticator_list.html`,
   `mfa/webauthn/add_form.html`, `mfa/webauthn/edit_form.html`,
   `mfa/webauthn/remove_form.html`, and `mfa/webauthn/reauthenticate.html`.

   Each override extends `app_base.html` directly (the same pattern as the
   TOTP and recovery-code overrides in `validibot/templates/mfa/`) and uses
   the `mfa_breadcrumbs` template tag to emit the top-bar breadcrumb trail.
   We do NOT extend allauth's `mfa/base_manage.html`, because Django block
   inheritance doesn't compose cleanly through it — allauth's leaf templates
   redefine `{% block content %}`, which erases any wrapper chrome we add at
   the base layer.

Also plan to add a **WebAuthn** card to `users/security.html` mirroring the
TOTP card before shipping, so users can discover and manage keys from the
Security page.

## Development bypass

If you're testing the login flow locally and don't want to keep a TOTP app
handy, you can set `MFA_TOTP_INSECURE_BYPASS_CODE` in a dev `.env` file to a
fixed six-digit string (e.g. `"000000"`). Allauth will accept that literal
code in place of a real TOTP. **Never set this in staging or production** —
anyone who knows the bypass code can complete MFA without the second factor.

## How the pages fit into Validibot chrome

Each allauth MFA leaf template has a Validibot-branded override in
`validibot/templates/mfa/`:

- `mfa/totp/activate_form.html`
- `mfa/totp/deactivate_form.html`
- `mfa/recovery_codes/index.html`
- `mfa/recovery_codes/generate.html`

All of them extend `app_base.html` directly (the same pattern as
`users/security.html`). None of them extend allauth's `mfa/base_manage.html`:
we tried that first, but Django block inheritance doesn't compose through it
— allauth's leaf templates redefine `{% block content %}`, which wipes out
any wrapper chrome added at the base layer.

Each override uses the `mfa_breadcrumbs` template tag
(`validibot/core/templatetags/core_tags.py`) to emit a
`User Settings › Security › {leaf}` trail into the top-bar breadcrumb slot.
The tag is there because allauth views don't run through our
`BreadcrumbMixin`, so the default breadcrumb partial would render nothing.

The `user_settings_nav_state` template tag (same file) keeps the
**Security** tab highlighted throughout the multi-step allauth flows by
matching any URL name prefixed with `mfa_`.

### The `mfa_index` redirect

Allauth ships its own MFA landing page at `/accounts/2fa/` (URL name
`mfa_index`) that duplicates the Security page with worse styling, and its
post-action flows hard-code `reverse("mfa_index")` as the redirect target
(e.g. after deactivating TOTP). `config/urls_web.py` preempts the
`mfa_index` URL name with a `RedirectView` pointing at `users:security`,
so every such redirect lands on our Security page instead. The override
is registered *before* the `accounts/` allauth URL include so Django's
first-match resolver picks ours.

## The Security landing page

`UserSecurityView` (`validibot/users/views.py`) renders
`templates/users/security.html` and is accessible at `/users/security/`. The
view computes a handful of context flags from the user's `Authenticator`
rows — `totp_enabled`, `is_mfa_enabled`, `recovery_codes` — so the template
can branch between "set up" and "deactivate" states without running its own
queries.

The view does not implement activation or deactivation itself. Those are
already handled by allauth's own URL names (`mfa_activate_totp`,
`mfa_deactivate_totp`, `mfa_generate_recovery_codes`, and so on), which the
Security page links to. Keeping the split this way means we inherit
allauth's hardening (rate-limiting, CSRF, session rotation) instead of
rewriting it.

## Testing

`validibot/users/tests/test_security.py` covers the Validibot-specific
wiring: access control on the landing page, context-flag correctness, that
both nav partials link to `users:security`, that the settings-nav tag
recognises the allauth `mfa_*` URL names, that each MFA leaf template
override extends `app_base.html` and emits the expected breadcrumb trail,
that the `mfa_breadcrumbs` tag returns the right shape, and that the
`mfa_index` URL redirects to the Security page without rendering
allauth's unbranded index template.

We deliberately don't re-test allauth's TOTP cryptography or state machine —
those live upstream and already have good coverage. If you add a new
authenticator type, the right place for tests is whatever renders the new
card on `security.html`, not the allauth plumbing underneath.

# Marketing Homepage Waitlist

The marketing homepage now features an HTMX-powered waitlist card that collects work
emails for beta announcements.

## Flow Overview

- **Template**: The section lives in `simplevalidations/templates/marketing/home.html`
  and renders the card via `marketing/partial/waitlist_form.html`.
- **Form**: `simplevalidations.marketing.forms.BetaWaitlistForm` validates email
  addresses, enforces a hidden honeypot field, and blocks common disposable domains.
- **View**: `simplevalidations.marketing.views.submit_beta_waitlist` accepts HTMX
  submissions and returns the form or success partial depending on validation.
- **Service**: `simplevalidations.marketing.services.submit_waitlist_signup`
  persists the signup to `simplevalidations.marketing.models.Prospect` and then sends
  a transactional welcome email using Django's `send_mail`.

## Data Model

Each signup creates (or updates) a `Prospect` record that captures the visitor's email,
origin (hero vs footer), source, referer, user agent, IP address, and the timestamp
when the welcome email was sent. The model is viewable in the Django admin for quick
follow-up or exports.

## Postmark Setup

Transactional email relies on the existing Anymail/Postmark configuration. Make sure
the following setting is present in environments where the waitlist should send email:

- `POSTMARK_SERVER_TOKEN`: Server token from the Postmark server configured in
  `config/settings/production.py`.

If the token is missing, Django falls back to the console email backend and the user
will see an error asking them to try again.

The metadata we send alongside the email includes the visitor's user agent, IP, and
referer so we can triage or segment follow-ups later.

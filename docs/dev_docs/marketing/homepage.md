# Marketing Homepage Waitlist

The marketing homepage now features an HTMX-powered waitlist card that collects work
emails for beta announcements.

## Flow Overview

- **Template**: The section lives in `simplevalidations/templates/marketing/home.html`
  and renders the card via `marketing/partial/waitlist_form.html`.
- **Form**: `simplevalidations.marketing.forms.BetaWaitlistForm` validates email
  addresses, enforces a hidden honeypot field, and blocks common disposable domains.
  The form configures a Crispy helper (Bootstrap 5 pack) so both the hero card and
  footer signup share the same markup; pass `origin="footer"` to stack the button
  under the input and to keep the HTMX target pointing at `#footer-waitlist`.
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

- `email_status` starts as `pending` after we send the welcome message, flips to
  `verified` when Postmark confirms delivery, and moves to `invalid` if Postmark
  reports a hard bounce. The webhook handlers live in
  `simplevalidations.marketing.views.postmark_delivery_webhook` and
  `...postmark_bounce_webhook`.
  Incoming webhook requests must originate from an IP in
  `POSTMARK_WEBHOOK_ALLOWED_IPS` (configurable via env var, defaults to Postmark’s
  documented ranges). On Heroku, we rely on the first address in
  `X-Forwarded-For`, which the platform populates with the original client IP.

## Postmark Setup

Transactional email relies on the existing Anymail/Postmark configuration. Make sure
the following setting is present in environments where the waitlist should send email:

- `POSTMARK_SERVER_TOKEN`: Server token from the Postmark server configured in
  `config/settings/production.py`.

If the token is missing, Django falls back to the console email backend and the user
will see an error asking them to try again.

The metadata we send alongside the email includes the visitor's user agent, IP, and
referer so we can triage or segment follow-ups later.

## Marketing Navigation Toggles

Set the following environment-driven flags in `config/settings/base.py` to control the
marketing navigation:

- `ENABLE_FEATURES_SECTION`
- `ENABLE_RESOURCES_SECTION`
- `ENABLE_DOCS_SECTION`
- `ENABLE_PRICING_SECTION`
- `ENABLE_BLOG`

## SEO Instrumentation

- Marketing views now inherit `MarketingMetadataMixin`, which sets `page_title`,
  `meta_description`, `meta_keywords`, a canonical URL, and structured data JSON-LD.
  Override those attributes or `get_structured_data()` in a view if a page needs
  custom metadata.
- `marketing_base.html` emits a canonical `<link>` plus JSON-LD containing the
  `WebSite`, `Organization`, and current `WebPage` schema objects.
- `config/urls.py` serves `sitemap.xml` (via `MarketingStaticViewSitemap`) and a
  dynamic `robots.txt` that announces the sitemap location. Add new marketing routes
  to `simplevalidations/marketing/sitemaps.py` whenever you introduce public pages.
- Published blog posts surface to crawlers through `BlogPostSitemap`; keep slugs stable
  once content goes live so Google Search retains the indexed URL.

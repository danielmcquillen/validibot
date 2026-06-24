/**
 * CSP-compatible reCAPTCHA v3 form handler.
 *
 * django-recaptcha's built-in widget renders inline <script> tags that
 * are blocked by our Content Security Policy (which requires nonces).
 * The widget still loads the external reCAPTCHA API from the whitelisted
 * Google CDN — only the inline JS handler is blocked.
 *
 * This module replaces that inline handler. It finds any reCAPTCHA v3
 * hidden input on the page, intercepts the form submit, calls
 * grecaptcha.execute() to get a token, and sets it on the hidden input
 * before allowing the form to submit.
 *
 * See: https://github.com/praekelt/django-recaptcha/issues/101
 */

declare const grecaptcha: {
  ready: (callback: () => void) => void;
  execute: (siteKey: string, options: { action?: string }) => Promise<string>;
};

export function initRecaptcha(): void {
  // Find all reCAPTCHA v3 hidden inputs on the page.
  const elements = document.querySelectorAll<HTMLInputElement>(
    'input.g-recaptcha[data-widget-uuid]',
  );

  if (elements.length === 0) {
    return;
  }

  // Wait for the reCAPTCHA API to load (it's loaded via the external
  // script tag that the widget still renders, which passes CSP because
  // the google.com domain is whitelisted).
  if (typeof grecaptcha === 'undefined') {
    // API not loaded yet — retry after a short delay.
    setTimeout(initRecaptcha, 500);
    return;
  }

  grecaptcha.ready(() => {
    elements.forEach((element) => {
      const form = element.form;
      if (!form) {
        return;
      }

      // Idempotency guard. initRecaptcha() runs more than once per page:
      // initAppFeatures() calls it from BOTH the DOMContentLoaded handler and
      // the htmx.onLoad callback, and htmx fires onLoad for the body on the
      // initial load too (the setTimeout retry above can also re-enter). Without
      // this guard the submit listener is attached multiple times, so a single
      // click kicks off several grecaptcha.execute() -> form.submit() chains and
      // the form POSTs more than once. On signup that second POST collides with
      // the just-created account and the user sees "A user with that username
      // already exists" (the duplicate-key path in users/forms.try_save). Mark
      // the form so exactly one handler is bound, while still allowing a
      // genuinely new form (e.g. one swapped in later by htmx) to get its own.
      if (form.dataset.recaptchaSubmitBound === 'true') {
        return;
      }
      form.dataset.recaptchaSubmitBound = 'true';

      const siteKey = element.getAttribute('data-sitekey') || '';
      const action = element.getAttribute('data-action') || 'submit';

      const onSubmit = (event: Event): void => {
        // If we already have a token, let the form submit normally.
        if (element.value) {
          return;
        }

        event.preventDefault();
        grecaptcha
          .execute(siteKey, { action })
          .then((token) => {
            element.value = token;
            form.submit();
          })
          .catch((error: unknown) => {
            // The challenge failed (network error, expired site key, quota).
            // We already called preventDefault(), so without recovering here
            // the user would click Submit and nothing would happen — a silent
            // dead form. We leave this submit listener INSTALLED so the next
            // click re-enters onSubmit, finds the token field still empty, and
            // retries grecaptcha.execute(). We deliberately do NOT remove the
            // listener or auto-submit without a token: removing it would let the
            // next submit post an empty token (UX regression — the form looks
            // submittable but silently fails the backend check), and a tight
            // auto-retry loop is worse than letting the user click again.
            console.error('reCAPTCHA challenge failed; please retry.', error);
          });
      };

      form.addEventListener('submit', onSubmit);
    });
  });
}

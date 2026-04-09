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

      const siteKey = element.getAttribute('data-sitekey') || '';
      const action = element.getAttribute('data-action') || 'submit';

      form.addEventListener('submit', (event) => {
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
          });
      });
    });
  });
}

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { initRecaptcha } from './recaptcha';

// Tests for the CSP-compatible reCAPTCHA v3 submit handler.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// The handler intercepts submit, calls grecaptcha.execute() for a token, then
// submits. The critical edge case: execute() can REJECT (network failure,
// expired site key, quota). Because the handler already called
// event.preventDefault(), a rejection that isn't handled leaves the form dead —
// the user clicks Submit and nothing happens, with no error. These tests pin
// the happy path (token set, form submitted) and the rejection path (form is
// re-enabled / not left silently wedged).

interface GrecaptchaMock {
    ready: (cb: () => void) => void;
    execute: (siteKey: string, options: { action?: string }) => Promise<string>;
}

function installGrecaptcha(mock: GrecaptchaMock): void {
    (globalThis as unknown as { grecaptcha: GrecaptchaMock }).grecaptcha = mock;
}

function buildRecaptchaForm(): {
    form: HTMLFormElement;
    input: HTMLInputElement;
} {
    const form = document.createElement('form');
    const input = document.createElement('input');
    input.className = 'g-recaptcha';
    input.setAttribute('data-widget-uuid', 'uuid-1');
    input.setAttribute('data-sitekey', 'site-key');
    input.setAttribute('data-action', 'submit');
    form.appendChild(input);
    document.body.appendChild(form);
    return { form, input };
}

describe('initRecaptcha', () => {
    beforeEach(() => {
        document.body.innerHTML = '';
    });

    afterEach(() => {
        vi.restoreAllMocks();
        delete (globalThis as unknown as { grecaptcha?: GrecaptchaMock }).grecaptcha;
    });

    it('sets the token and submits the form on a successful challenge', async () => {
        const { form, input } = buildRecaptchaForm();
        installGrecaptcha({
            ready: (cb) => cb(),
            execute: () => Promise.resolve('tok-123'),
        });
        const submitSpy = vi
            .spyOn(form, 'submit')
            .mockImplementation(() => undefined);

        initRecaptcha();
        form.dispatchEvent(new Event('submit', { cancelable: true }));

        // execute() resolves on a microtask; flush it.
        await Promise.resolve();
        await Promise.resolve();

        expect(input.value).toBe('tok-123');
        expect(submitSpy).toHaveBeenCalledTimes(1);
    });

    it('surfaces the failure and does not submit when the challenge rejects', async () => {
        // The regression test for the silent-dead-form bug: execute() rejects,
        // and the handler has already preventDefault()'d. It must not throw an
        // unhandled rejection, must not set a token, and must not submit an
        // empty token — it logs and waits for the user to retry.
        const { form, input } = buildRecaptchaForm();
        installGrecaptcha({
            ready: (cb) => cb(),
            execute: () => Promise.reject(new Error('network down')),
        });
        const submitSpy = vi
            .spyOn(form, 'submit')
            .mockImplementation(() => undefined);
        const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

        initRecaptcha();
        form.dispatchEvent(new Event('submit', { cancelable: true }));

        await Promise.resolve();
        await Promise.resolve();

        expect(input.value).toBe('');
        expect(submitSpy).not.toHaveBeenCalled();
        expect(errorSpy).toHaveBeenCalled();
    });

    it('retries on the next submit after a transient failure, then succeeds', async () => {
        // The core recovery guarantee: a transient reCAPTCHA failure must not
        // leave the form permanently broken. The submit listener stays bound, so
        // a second click re-runs execute(). Here the first challenge rejects and
        // the second resolves — the form must then receive the token and submit.
        // This is the exact scenario the silent-dead-form fix exists to handle.
        const { form, input } = buildRecaptchaForm();
        const execute = vi
            .fn<(siteKey: string, options: { action?: string }) => Promise<string>>()
            .mockRejectedValueOnce(new Error('network down'))
            .mockResolvedValueOnce('tok-retry');
        installGrecaptcha({ ready: (cb) => cb(), execute });
        const submitSpy = vi
            .spyOn(form, 'submit')
            .mockImplementation(() => undefined);
        vi.spyOn(console, 'error').mockImplementation(() => undefined);

        initRecaptcha();

        // First submit: challenge rejects, nothing submitted.
        form.dispatchEvent(new Event('submit', { cancelable: true }));
        await Promise.resolve();
        await Promise.resolve();
        expect(submitSpy).not.toHaveBeenCalled();
        expect(input.value).toBe('');

        // Second submit (user retries): same listener fires, challenge resolves.
        form.dispatchEvent(new Event('submit', { cancelable: true }));
        await Promise.resolve();
        await Promise.resolve();

        expect(execute).toHaveBeenCalledTimes(2);
        expect(input.value).toBe('tok-retry');
        expect(submitSpy).toHaveBeenCalledTimes(1);
    });
});

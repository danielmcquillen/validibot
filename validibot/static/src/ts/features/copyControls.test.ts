import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { initCopyControls } from './copyControls';

function buildCopyDocument(markup = `
    <input id="api-key-value" value="vbk_1_secret">
    <button
      type="button"
      data-copy-target="#api-key-value"
      data-copy-timeout="1600"
      data-copied-label="Copied"
    >Copy</button>
`): {
    doc: Document;
    button: HTMLButtonElement;
} {
    const doc = document.implementation.createHTMLDocument('copy controls test');
    doc.body.innerHTML = markup;
    const button = doc.querySelector<HTMLButtonElement>('[data-copy-target]');
    if (!button) {
        throw new Error('Test markup did not include a copy button.');
    }
    return { doc, button };
}

function scheduleWithWindowTimer(handler: () => void, timeout: number): number {
    return window.setTimeout(handler, timeout);
}

async function flushCopyHandler(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
}

describe('initCopyControls', () => {
    beforeEach(() => {
        vi.useFakeTimers();
    });

    afterEach(() => {
        vi.useRealTimers();
        vi.restoreAllMocks();
    });

    it('copies an input value and restores the default label after the timeout', async () => {
        const { doc, button } = buildCopyDocument();
        const copyText = vi.fn(() => Promise.resolve(true));

        initCopyControls({
            root: doc,
            copyText,
            setTimeout: scheduleWithWindowTimer,
        });
        button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await flushCopyHandler();

        expect(copyText).toHaveBeenCalledWith('vbk_1_secret');
        expect(button.textContent).toBe('Copied');

        vi.advanceTimersByTime(1600);

        expect(button.textContent).toBe('Copy');
    });

    it('copies text content when the target is not a form field', async () => {
        const { doc, button } = buildCopyDocument(`
            <code id="api-key-value">vbk_1_from_text</code>
            <button type="button" data-copy-target="#api-key-value">Copy</button>
        `);
        const copyText = vi.fn(() => Promise.resolve(true));

        initCopyControls({
            root: doc,
            copyText,
            setTimeout: scheduleWithWindowTimer,
        });
        button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await flushCopyHandler();

        expect(copyText).toHaveBeenCalledWith('vbk_1_from_text');
        expect(button.textContent).toBe('Copied');
    });

    it('does nothing when the target field is missing or empty', async () => {
        const { doc, button } = buildCopyDocument(`
            <input id="api-key-value" value="   ">
            <button type="button" data-copy-target="#api-key-value">Copy</button>
            <button type="button" data-copy-target="#missing">Missing</button>
        `);
        const missingButton = doc.querySelectorAll<HTMLButtonElement>('[data-copy-target]')[1];
        const copyText = vi.fn(() => Promise.resolve(true));

        initCopyControls({
            root: doc,
            copyText,
            setTimeout: scheduleWithWindowTimer,
        });
        button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        missingButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await flushCopyHandler();

        expect(copyText).not.toHaveBeenCalled();
        expect(button.textContent).toBe('Copy');
        expect(missingButton.textContent).toBe('Missing');
    });

    it('leaves the label unchanged when copy fails', async () => {
        const { doc, button } = buildCopyDocument();
        const copyText = vi.fn(() => Promise.resolve(false));

        initCopyControls({
            root: doc,
            copyText,
            setTimeout: scheduleWithWindowTimer,
        });
        button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await flushCopyHandler();

        expect(copyText).toHaveBeenCalledWith('vbk_1_secret');
        expect(button.textContent).toBe('Copy');
    });

    it('binds only once per root', async () => {
        const { doc, button } = buildCopyDocument();
        const copyText = vi.fn(() => Promise.resolve(true));

        initCopyControls({
            root: doc,
            copyText,
            setTimeout: scheduleWithWindowTimer,
        });
        initCopyControls({
            root: doc,
            copyText,
            setTimeout: scheduleWithWindowTimer,
        });
        button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await flushCopyHandler();

        expect(copyText).toHaveBeenCalledTimes(1);
    });

    it('swallows rejected copy attempts without changing the label', async () => {
        const { doc, button } = buildCopyDocument();
        const copyText = vi.fn(() => Promise.reject(new Error('clipboard denied')));

        initCopyControls({
            root: doc,
            copyText,
            setTimeout: scheduleWithWindowTimer,
        });
        button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await flushCopyHandler();

        expect(copyText).toHaveBeenCalledWith('vbk_1_secret');
        expect(button.textContent).toBe('Copy');
    });
});

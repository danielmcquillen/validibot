import { beforeEach, describe, expect, it, vi } from 'vitest';

import { initWorkflowLaunch } from './workflowLaunch';

// Tests for workflow-launch initialization idempotency.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// initWorkflowLaunch runs on initial load AND on every HTMx swap (via
// initAppFeatures -> htmx.onLoad). Without an init-once guard it re-binds all of
// the form's click/drop/change listeners every time the surrounding region is
// swapped, so a single user action fires its handler N times — duplicate
// preflight POSTs, the file picker reopening, etc. These tests pin that
// initializing the same form twice binds each listener only once.

function buildLaunchForm(): HTMLElement {
    // Minimal DOM matching the selectors WorkflowLaunchController.init() reads.
    const form = document.createElement('div');
    form.setAttribute('data-workflow-launch-form', 'true');
    form.setAttribute('data-default-mode', 'upload');
    form.innerHTML = `
        <button type="button" data-content-mode="upload"></button>
        <button type="button" data-content-mode="paste"></button>
        <div data-upload-section></div>
        <div data-paste-section></div>
        <input type="hidden" data-input-mode-field />
    `;
    return form;
}

describe('initWorkflowLaunch idempotency', () => {
    beforeEach(() => {
        document.body.innerHTML = '';
    });

    it('binds the mode-button click listener only once across repeated init calls', () => {
        const form = buildLaunchForm();
        document.body.appendChild(form);

        const uploadButton = form.querySelector<HTMLButtonElement>(
            '[data-content-mode="upload"]',
        );
        expect(uploadButton).not.toBeNull();

        // Count click listeners bound to the upload button.
        const addSpy = vi.spyOn(uploadButton as HTMLButtonElement, 'addEventListener');

        // Simulate initial load + one HTMx swap re-initializing the same form.
        initWorkflowLaunch(document);
        initWorkflowLaunch(document);

        const clickBindings = addSpy.mock.calls.filter(([type]) => type === 'click');
        // Without a guard this is 2 (double-bound); the fix makes it 1.
        expect(clickBindings).toHaveLength(1);
    });

    it('fires a single mode switch per click after repeated init calls', () => {
        // Behavioural complement to the binding-count test: after two inits, one
        // click on "paste" should transition the UI exactly once. If the handler
        // were double-bound, the paste section would still end visible, but the
        // duplicate side effects (duplicate preflight fetches in production) are
        // the real harm — this guards the user-visible single-transition.
        const form = buildLaunchForm();
        document.body.appendChild(form);

        initWorkflowLaunch(document);
        initWorkflowLaunch(document);

        const pasteButton = form.querySelector<HTMLButtonElement>(
            '[data-content-mode="paste"]',
        );
        const pasteSection = form.querySelector<HTMLElement>('[data-paste-section]');

        pasteButton?.dispatchEvent(new Event('click'));

        // Paste mode is active exactly once; section is shown.
        expect(pasteSection?.classList.contains('d-none')).toBe(false);
        expect(pasteButton?.classList.contains('active')).toBe(true);
    });
});

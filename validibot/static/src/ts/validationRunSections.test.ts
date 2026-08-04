import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
    initValidationRunSections,
    VALIDATION_RUN_SECTION_STORAGE_PREFIX,
} from './validationRunSections';

// Tests for validation-run accordion preferences.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// These panels are re-rendered both by normal navigation and HTMX swaps. The
// tests pin the user-facing contract: all panels start closed, each section's
// state is independent and session-scoped, restored state updates visual and
// accessible markup together, and repeated initialization never duplicates
// event handlers.

function addSection(
    key: string,
    initiallyOpen = false,
): { section: HTMLElement; body: HTMLElement; toggle: HTMLButtonElement } {
    const section = document.createElement('div');
    section.dataset.validationRunSection = key;
    section.innerHTML = `
        <button
          class="accordion-button${initiallyOpen ? '' : ' collapsed'}"
          aria-expanded="${initiallyOpen ? 'true' : 'false'}"
          data-validation-run-section-toggle
        ></button>
        <div
          class="accordion-collapse collapse${initiallyOpen ? ' show' : ''}"
          data-validation-run-section-body
        ></div>
    `;
    document.body.appendChild(section);
    return {
        section,
        body: section.querySelector<HTMLElement>(
            '[data-validation-run-section-body]',
        )!,
        toggle: section.querySelector<HTMLButtonElement>(
            '[data-validation-run-section-toggle]',
        )!,
    };
}

describe('initValidationRunSections', () => {
    beforeEach(() => {
        document.body.innerHTML = '';
        window.sessionStorage.clear();
        vi.restoreAllMocks();
    });

    it('closes every section when the session has no saved preferences', () => {
        /** First-time reports must be compact even if stale markup says open. */
        const outputs = addSection('outputs', true);
        const evidence = addSection('evidence-manifest', true);

        initValidationRunSections();

        for (const { body, toggle } of [outputs, evidence]) {
            expect(body.classList.contains('show')).toBe(false);
            expect(toggle.classList.contains('collapsed')).toBe(true);
            expect(toggle.getAttribute('aria-expanded')).toBe('false');
        }
    });

    it('restores open and closed preferences independently', () => {
        /** Expanding Outputs must not implicitly expand any proof panel. */
        window.sessionStorage.setItem(
            VALIDATION_RUN_SECTION_STORAGE_PREFIX + 'outputs',
            'open',
        );
        window.sessionStorage.setItem(
            VALIDATION_RUN_SECTION_STORAGE_PREFIX + 'signed-credential',
            'closed',
        );
        const outputs = addSection('outputs');
        const credential = addSection('signed-credential', true);

        initValidationRunSections();

        expect(outputs.body.classList.contains('show')).toBe(true);
        expect(outputs.toggle.classList.contains('collapsed')).toBe(false);
        expect(outputs.toggle.getAttribute('aria-expanded')).toBe('true');
        expect(credential.body.classList.contains('show')).toBe(false);
        expect(credential.toggle.classList.contains('collapsed')).toBe(true);
        expect(credential.toggle.getAttribute('aria-expanded')).toBe('false');
    });

    it('persists Bootstrap open and close events for the next run', () => {
        /** Navigation should restore the last completed user interaction. */
        const { body } = addSection('user-inputs');
        initValidationRunSections();

        body.dispatchEvent(new Event('show.bs.collapse'));
        expect(
            window.sessionStorage.getItem(
                VALIDATION_RUN_SECTION_STORAGE_PREFIX + 'user-inputs',
            ),
        ).toBe('open');

        body.dispatchEvent(new Event('hide.bs.collapse'));
        expect(
            window.sessionStorage.getItem(
                VALIDATION_RUN_SECTION_STORAGE_PREFIX + 'user-inputs',
            ),
        ).toBe('closed');
    });

    it('binds storage listeners only once across repeated HTMX initialization', () => {
        /** HTMX may initialize an unchanged root more than once. */
        const { body } = addSection('outputs');
        const addListener = vi.spyOn(body, 'addEventListener');

        initValidationRunSections();
        initValidationRunSections();

        expect(
            addListener.mock.calls.filter(([eventName]) =>
                eventName === 'show.bs.collapse' || eventName === 'hide.bs.collapse'
            ),
        ).toHaveLength(2);
    });

    it('keeps the default closed state when session storage is unavailable', () => {
        /** Privacy settings must not prevent the report itself from rendering. */
        const storage = window.sessionStorage;
        vi.spyOn(storage, 'getItem').mockImplementation(() => {
            throw new Error('session storage disabled');
        });
        const { body, toggle } = addSection('outputs', true);

        expect(() => initValidationRunSections(document, storage)).not.toThrow();
        expect(body.classList.contains('show')).toBe(false);
        expect(toggle.getAttribute('aria-expanded')).toBe('false');
    });
});

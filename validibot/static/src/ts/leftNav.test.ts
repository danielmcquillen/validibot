import { beforeEach, describe, expect, it } from 'vitest';

import { initAppLeftNavToggle } from './leftNav';
import { LEFT_NAV_PREF_ATTRIBUTE, LEFT_NAV_STORAGE_KEY } from './leftNavConstants';

// Tests for the interactive left-nav collapse toggle.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// This is the JS that actually controls the left nav in the rendered page: it
// reads the stored preference on load, toggles the collapsed class + ARIA on
// click, persists the new preference, and keeps the <html> attribute in sync
// (so the before-paint headInit snippet and the live toggle agree). We drive it
// against a jsdom DOM that mirrors the real markup (#app-left-nav,
// #app-left-nav-toggle) and assert the observable effects.

function buildNavDom(): {
    nav: HTMLElement;
    toggle: HTMLButtonElement;
} {
    const nav = document.createElement('nav');
    nav.id = 'app-left-nav';

    const toggle = document.createElement('button');
    toggle.id = 'app-left-nav-toggle';
    toggle.dataset.collapsedLabel = 'Show navigation';
    toggle.dataset.expandedLabel = 'Hide navigation';

    document.body.append(nav, toggle);
    return { nav, toggle };
}

describe('initAppLeftNavToggle', () => {
    beforeEach(() => {
        document.body.innerHTML = '';
        document.documentElement.removeAttribute(LEFT_NAV_PREF_ATTRIBUTE);
        window.localStorage.clear();
    });

    it('is a no-op when the nav or toggle is absent', () => {
        // Pages without the left nav (e.g. auth screens) must not error.
        expect(() => initAppLeftNavToggle()).not.toThrow();
    });

    it('starts collapsed when the stored preference says collapsed', () => {
        window.localStorage.setItem(LEFT_NAV_STORAGE_KEY, '1');
        const { nav, toggle } = buildNavDom();

        initAppLeftNavToggle();

        expect(nav.classList.contains('is-collapsed')).toBe(true);
        expect(nav.getAttribute('aria-hidden')).toBe('true');
        expect(toggle.getAttribute('aria-expanded')).toBe('false');
        expect(toggle.getAttribute('aria-label')).toBe('Show navigation');
        expect(document.documentElement.getAttribute(LEFT_NAV_PREF_ATTRIBUTE)).toBe('true');
    });

    it('starts expanded when there is no stored preference', () => {
        const { nav, toggle } = buildNavDom();

        initAppLeftNavToggle();

        expect(nav.classList.contains('is-collapsed')).toBe(false);
        expect(nav.getAttribute('aria-hidden')).toBe('false');
        expect(toggle.getAttribute('aria-expanded')).toBe('true');
        expect(document.documentElement.hasAttribute(LEFT_NAV_PREF_ATTRIBUTE)).toBe(false);
    });

    it('collapses on click and persists the preference', () => {
        const { nav, toggle } = buildNavDom();
        initAppLeftNavToggle();

        toggle.dispatchEvent(new Event('click'));

        expect(nav.classList.contains('is-collapsed')).toBe(true);
        expect(toggle.getAttribute('aria-expanded')).toBe('false');
        expect(window.localStorage.getItem(LEFT_NAV_STORAGE_KEY)).toBe('1');
        expect(document.documentElement.getAttribute(LEFT_NAV_PREF_ATTRIBUTE)).toBe('true');
    });

    it('toggles back to expanded on a second click and clears the attribute', () => {
        const { nav, toggle } = buildNavDom();
        initAppLeftNavToggle();

        toggle.dispatchEvent(new Event('click')); // collapse
        toggle.dispatchEvent(new Event('click')); // expand

        expect(nav.classList.contains('is-collapsed')).toBe(false);
        expect(toggle.getAttribute('aria-expanded')).toBe('true');
        expect(window.localStorage.getItem(LEFT_NAV_STORAGE_KEY)).toBe('0');
        expect(document.documentElement.hasAttribute(LEFT_NAV_PREF_ATTRIBUTE)).toBe(false);
    });
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { applyInitialLeftNavState } from './headInit';
import { LEFT_NAV_PREF_ATTRIBUTE, LEFT_NAV_STORAGE_KEY } from './leftNavConstants';

// Tests for the before-paint head-init snippet.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// This snippet runs synchronously in <head> to prime the collapsed-nav state
// before first paint (avoiding a flash of the wrong UI). The behaviour that
// matters: it stamps the <html> attribute when the stored preference says
// "collapsed", leaves it alone otherwise, and never throws when localStorage
// is unavailable (privacy mode) — because a throw in <head> would block the
// whole page render.

describe('applyInitialLeftNavState', () => {
    beforeEach(() => {
        // Each test starts from a clean DOM + storage.
        document.documentElement.removeAttribute(LEFT_NAV_PREF_ATTRIBUTE);
        window.localStorage.clear();
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('stamps the prefers-collapsed attribute when the stored preference is collapsed', () => {
        // The user previously collapsed the nav, so before paint we must mark
        // <html> so CSS renders it collapsed immediately.
        window.localStorage.setItem(LEFT_NAV_STORAGE_KEY, '1');

        applyInitialLeftNavState();

        expect(
            document.documentElement.getAttribute(LEFT_NAV_PREF_ATTRIBUTE),
        ).toBe('true');
    });

    it('leaves the attribute unset when the preference is not collapsed', () => {
        // Default / expanded state must not stamp the attribute, or the nav
        // would render collapsed against the user's preference.
        window.localStorage.setItem(LEFT_NAV_STORAGE_KEY, '0');

        applyInitialLeftNavState();

        expect(
            document.documentElement.hasAttribute(LEFT_NAV_PREF_ATTRIBUTE),
        ).toBe(false);
    });

    it('leaves the attribute unset when there is no stored preference', () => {
        // First-ever visit: nothing stored, nav stays expanded.
        applyInitialLeftNavState();

        expect(
            document.documentElement.hasAttribute(LEFT_NAV_PREF_ATTRIBUTE),
        ).toBe(false);
    });

    it('does not throw when localStorage access fails (privacy mode)', () => {
        // A throw here would run in <head> and block the page from rendering,
        // so the snippet must swallow storage errors. We force getItem to throw
        // and assert the call still completes and stamps nothing.
        vi.spyOn(window.localStorage, 'getItem').mockImplementation(() => {
            throw new Error('localStorage disabled');
        });

        expect(() => applyInitialLeftNavState()).not.toThrow();
        expect(
            document.documentElement.hasAttribute(LEFT_NAV_PREF_ATTRIBUTE),
        ).toBe(false);
    });
});

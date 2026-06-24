// Left-nav collapse toggle.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// Wires the "collapse/expand the left navigation" button and persists the
// preference across pages. Extracted from project.ts so the behaviour is
// testable in isolation (project.ts is a side-effect entry point that wires the
// whole app on import, which is impractical to drive from a unit test).
//
// The before-paint priming of this same preference lives in headInit.ts, which
// runs synchronously in <head>; this module runs from the deferred bundle and
// owns the interactive toggle. Both share LEFT_NAV_STORAGE_KEY /
// LEFT_NAV_PREF_ATTRIBUTE from leftNavConstants.ts.

import { LEFT_NAV_PREF_ATTRIBUTE, LEFT_NAV_STORAGE_KEY } from './leftNavConstants';

export function initAppLeftNavToggle(): void {
    const nav = document.getElementById('app-left-nav');
    const toggle = document.getElementById('app-left-nav-toggle') as HTMLButtonElement | null;

    if (!nav || !toggle) {
        return;
    }

    const collapsedClass = 'is-collapsed';
    const collapsedLabel = toggle.dataset.collapsedLabel ?? 'Show navigation';
    const expandedLabel = toggle.dataset.expandedLabel ?? 'Hide navigation';

    const applyState = (collapsed: boolean) => {
        nav.classList.toggle(collapsedClass, collapsed);
        nav.setAttribute('aria-hidden', collapsed ? 'true' : 'false');
        toggle.setAttribute('aria-expanded', (!collapsed).toString());
        toggle.setAttribute('aria-label', collapsed ? collapsedLabel : expandedLabel);
        if (collapsed) {
            document.documentElement?.setAttribute(LEFT_NAV_PREF_ATTRIBUTE, 'true');
        } else {
            document.documentElement?.removeAttribute(LEFT_NAV_PREF_ATTRIBUTE);
        }
    };

    let startCollapsed = false;
    try {
        startCollapsed = window.localStorage.getItem(LEFT_NAV_STORAGE_KEY) === '1';
    } catch (error) {
        console.debug('Unable to read left nav toggle preference', error);
    }
    applyState(startCollapsed);

    toggle.addEventListener('click', () => {
        const collapsed = !nav.classList.contains(collapsedClass);
        applyState(collapsed);
        try {
            window.localStorage.setItem(LEFT_NAV_STORAGE_KEY, collapsed ? '1' : '0');
        } catch (error) {
            console.debug('Unable to persist left nav toggle preference', error);
        }
    });
}

// Synchronous <head> initialization — runs BEFORE first paint.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// This file is compiled to its own tiny, dependency-free file (head-init.js)
// and inlined into <head> via the {% head_init_script %} template tag. It must
// stay small and side-effect-only: it runs synchronously before the page is
// painted, so it can prime visual state without a flash of the wrong UI (FOUC).
//
// The main bundle (project.js) is loaded with `defer` and therefore runs after
// parse/paint — too late to prevent the flash — which is exactly why this
// snippet exists separately and runs in <head> instead of being folded into
// project.ts.
//
// Keep this list of responsibilities deliberately short. Anything that can wait
// until after paint belongs in project.ts, not here.

import { LEFT_NAV_PREF_ATTRIBUTE, LEFT_NAV_STORAGE_KEY } from './leftNavConstants';

/**
 * Prime the collapsed-nav preference on <html> before paint.
 *
 * Reads the persisted preference and, if the user last left the nav collapsed,
 * stamps the attribute CSS keys off so the nav renders collapsed immediately
 * rather than expanding then snapping shut once the deferred bundle runs.
 */
export function applyInitialLeftNavState(): void {
    try {
        if (window.localStorage.getItem(LEFT_NAV_STORAGE_KEY) === '1') {
            document.documentElement.setAttribute(LEFT_NAV_PREF_ATTRIBUTE, 'true');
        }
    } catch (error) {
        // localStorage can throw (privacy mode, disabled storage). A failure
        // here is non-fatal: the nav simply starts expanded and the deferred
        // bundle still wires the toggle.
        console.debug('Unable to prime left-nav preference', error);
    }
}

applyInitialLeftNavState();

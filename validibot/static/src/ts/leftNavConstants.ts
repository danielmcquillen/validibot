// Shared left-nav preference constants.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// These are the single source of truth for how the collapsed-nav preference is
// stored and signalled. Two places read them:
//
//   1. project.ts (initAppLeftNavToggle) — the deferred main bundle that wires
//      the toggle button and reads/writes the stored preference.
//   2. headInit.ts — a tiny snippet compiled to its own file and inlined
//      synchronously in <head>, BEFORE first paint, so the nav doesn't flash
//      open then collapse (FOUC). The deferred bundle runs too late for that.
//
// Keeping the key and attribute name here means a rename can't silently break
// one copy: both importers fail to compile together if this changes. Previously
// the head snippet was hand-written inline in base.html and duplicated these
// literals, so a rename in project.ts would have left the inline snippet
// reading a stale key.

// Persist the user's collapse preference across pages.
export const LEFT_NAV_STORAGE_KEY = 'validibot:leftNavCollapsed';

// Attribute set on <html> to let CSS render the collapsed state before paint.
export const LEFT_NAV_PREF_ATTRIBUTE = 'data-left-nav-prefers-collapsed';

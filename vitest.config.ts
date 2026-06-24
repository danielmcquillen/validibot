import { defineConfig } from 'vitest/config';

// Vitest configuration for the front-end TypeScript suite.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// We test the browser-facing TS modules in static/src/ts. The jsdom environment
// gives us a real DOM (document, localStorage, addEventListener) so we can
// verify DOM-manipulating logic — toggle state, init-once guards, event
// binding — without launching a browser. This catches the bulk of front-end
// behaviour bugs in CI quickly; true paint/CSS issues still need a browser test.
//
// Vitest uses esbuild under the hood, the same bundler the build pipeline uses,
// so TypeScript runs with no extra transform config.
export default defineConfig({
    test: {
        // jsdom provides document/window/localStorage for DOM tests.
        environment: 'jsdom',
        // jsdom only exposes localStorage when the document has a real origin
        // (storage is partitioned by origin; about:blank has none). Setting a
        // concrete URL activates window.localStorage for the tests.
        environmentOptions: {
            jsdom: { url: 'http://localhost/' },
        },
        // Installs an in-memory localStorage stub (jsdom doesn't reliably
        // provide one in this headless setup). See test-setup.ts.
        setupFiles: ['validibot/static/src/ts/test-setup.ts'],
        // Co-locate tests with the modules they cover (foo.test.ts next to foo.ts).
        include: ['validibot/static/src/ts/**/*.test.ts'],
        // Keep the runner focused on our source; never descend into deps.
        exclude: ['node_modules/**', 'validibot/static/js/**'],
        globals: true,
    },
});

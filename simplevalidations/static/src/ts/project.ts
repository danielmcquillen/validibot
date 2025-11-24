// Import all javascript libraries and functions and css styles.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import * as bootstrap from 'bootstrap';
import { Chart, registerables } from 'chart.js';
import htmx from 'htmx.org';
import { initAppFeatures } from './app';
import { initTableSorting } from './tableSorting';

declare global {
    interface Window {
        bootstrap: typeof bootstrap;
        htmx: typeof htmx;
        Chart: typeof Chart;
    }
}
window.bootstrap = bootstrap;
window.htmx = htmx;
Chart.register(...registerables);
window.Chart = Chart;
import 'htmx.org';

function initializeCharts(root: ParentNode | Document = document): void {
    const chartCanvases = root.querySelectorAll<HTMLCanvasElement>('canvas[data-chart-config-id]');
    chartCanvases.forEach((canvas) => {
        const configId = canvas.dataset.chartConfigId;
        if (!configId) {
            return;
        }

        const existing = Chart.getChart(canvas);
        if (existing) {
            existing.destroy();
        }

        const scriptElement = document.getElementById(configId) as HTMLScriptElement | null;
        if (!scriptElement) {
            return;
        }

        try {
            const config = JSON.parse(scriptElement.textContent || '{}');
            new Chart(canvas, config);
            canvas.dataset.chartInitialized = '1';
        } catch (err) {
            console.error('Error initialising dashboard chart', err);
        }
    });
}

const LEFT_NAV_STORAGE_KEY = 'simplevalidations:leftNavCollapsed'; // Persist the user's collapse preference across pages.
const LEFT_NAV_PREF_ATTRIBUTE = 'data-left-nav-prefers-collapsed';

function initAppLeftNavToggle(): void {
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

window.addEventListener('DOMContentLoaded', () => {

    console.log("DOM fully loaded and parsed....");

    // Bootstrap setup...
    simplevalidationsInitBootstrap();
    initAppLeftNavToggle();

    // HTMX global event listeners for disabling submit and showing spinner
    htmx.on('htmx:beforeRequest', (event: Event) => {
        const target = event.target;
        if (!(target instanceof HTMLElement) || !target.matches('.assessment-form')) {
            return;
        }
        const submitButton = target.querySelector<HTMLButtonElement>('button[type=submit]');
        if (submitButton) {
            submitButton.disabled = true;
            submitButton.innerHTML =
                '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Submitting...';
        }
    });

    initializeCharts(document);
    initAppFeatures(document);
    initTableSorting(document);
});

function simplevalidationsInitBootstrap() {
    try {

        console.log("Enabling bootstrap toasts...")
        const toastElements = Array.from(document.querySelectorAll<HTMLElement>('.toast'));
        const toastList = toastElements.map((toastEl) => new bootstrap.Toast(toastEl));
        toastList.forEach((toast) => toast.show());

        console.log("Enabling bootstrap tooltips...")
        const tooltipTriggerList = Array.from(
            document.querySelectorAll<HTMLElement>('[data-bs-toggle="tooltip"]'),
        );
        tooltipTriggerList.forEach((tooltipTriggerEl) => {
            new bootstrap.Tooltip(tooltipTriggerEl);
        });
    } catch (err) {
        console.log("Error initializing bootstrap: ", err)
    }
}



document.addEventListener("DOMContentLoaded", () => {
    const navbar = document.getElementById("site-top-nav");
    if (!navbar) {
        return;
    }
    let lastScroll = window.pageYOffset || document.documentElement.scrollTop;

    window.addEventListener("scroll", () => {
        const currentScroll = window.pageYOffset || document.documentElement.scrollTop;

        if (currentScroll > lastScroll) {
            // Scrolling down: hide navbar
            navbar!.classList.add("navbar-hidden");
        } else {
            // Scrolling up: show navbar
            navbar!.classList.remove("navbar-hidden");
        }

        // Prevent negative scroll values
        lastScroll = currentScroll <= 0 ? 0 : currentScroll;
    });
});

document.body.addEventListener('htmx:beforeSwap', () => {
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
        const tooltip = bootstrap.Tooltip.getInstance(el);
        if (tooltip) {
            tooltip.hide();
        }
    });
});

type QueryableRoot = ParentNode & Node;

function resolveRoot(node: Node | null | undefined): QueryableRoot {
    if (node && 'querySelectorAll' in (node as ParentNode)) {
        return node as QueryableRoot;
    }
    return document;
}

window.htmx.onLoad((content: Node) => {
    const root = resolveRoot(content);
    initializeCharts(root);
    initAppFeatures(root);
    initTableSorting(root);

    root.querySelectorAll<HTMLElement>('[data-bs-toggle="tooltip"]').forEach((tooltipTriggerEl) => {
        new window.bootstrap.Tooltip(tooltipTriggerEl);
    });

    root.querySelectorAll<HTMLElement>('[data-bs-toggle="collapse"]').forEach((trigger) => {
        const targetSelector = trigger.getAttribute('data-bs-target');
        if (!targetSelector) {
            return;
        }
        const collapseEl = document.querySelector<HTMLElement>(targetSelector);
        if (collapseEl && !window.bootstrap.Collapse.getInstance(collapseEl)) {
            new window.bootstrap.Collapse(collapseEl, { toggle: false });
        }
    });

    root.querySelectorAll<HTMLElement>('.toast').forEach((toastEl) => {
        const toast = new bootstrap.Toast(toastEl);
        toast.show();
    });
});

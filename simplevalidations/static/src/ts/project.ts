// Import all javascript libraries and functions.
// This 'project.ts' file is meant for javascript, typescript and css/scss
// that is used across the entire project.

// ( app.ts holds code specific to the main app interface )
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import * as bootstrap from 'bootstrap';
import { Chart, registerables } from 'chart.js';
import htmx from 'htmx.org';

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

window.addEventListener('DOMContentLoaded', (event) => {

    console.log("DOM fully loaded and parsed....");

    // Bootstrap setup...
    simplevalidationsInitBootstrap();

    // HTMX global event listeners for disabling submit and showing spinner
    htmx.on('htmx:beforeRequest', (evt: any) => {
        const target = evt.target as HTMLElement;
        if (target.matches('.assessment-form')) {
            const btn = target.querySelector('button[type=submit]') as HTMLButtonElement;
            if (btn) {
                btn.disabled = true;
                btn.innerHTML =
                    '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Submitting...';
            }
        }
    });

    initializeCharts(document);
});

function simplevalidationsInitBootstrap() {
    try {

        console.log("Enabling bootstrap toasts...")
        let toastElList = [].slice.call(document.querySelectorAll('.toast'))
        let toastList = toastElList.map(function (toastEl) {
            return new bootstrap.Toast(toastEl, {});
        }
        )
        toastList.forEach(toast => toast.show());

        console.log("Enabling bootstrap tooltips...")
        let tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'))
        let tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
            return new bootstrap.Tooltip(tooltipTriggerEl)
        })
    } catch (err) {
        console.log("Error initializing bootstrap: ", err)
    }
}



document.addEventListener("DOMContentLoaded", () => {
    const navbar = document.getElementById("site-top-nav");
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

document.body.addEventListener('htmx:beforeSwap', function (evt) {
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
        var tooltip = bootstrap.Tooltip.getInstance(el);
        if (tooltip) {
            tooltip.hide();
        }
    });
});

document.body.addEventListener('htmx:afterSwap', function (evt: any) {
    // evt.detail.target is the element that received the new content
    const newContent = evt.detail.target;
    // Find elements in new content that need tooltips
    const tooltipTriggerList = [].slice.call(newContent.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.forEach(function (tooltipTriggerEl) {
        // Create a new tooltip instance for each element
        new window.bootstrap.Tooltip(tooltipTriggerEl);
    });
});

document.body.addEventListener('htmx:afterSwap', function (evt: any) {
    // Look for any new collapse triggers in the swapped content
    console.log("wiring up collapse triggers")
    const container = evt.detail.target;
    container.querySelectorAll('[data-bs-toggle="collapse"]').forEach(trigger => {
        // Get the target selector from the trigger's data attribute
        const targetSelector = trigger.getAttribute('data-bs-target');
        if (targetSelector) {
            const collapseEl = container.querySelector(targetSelector);
            if (collapseEl) {
                // Only create a new collapse instance if one doesn't already exist
                if (!window.bootstrap.Collapse.getInstance(collapseEl)) {
                    new window.bootstrap.Collapse(collapseEl, {
                        toggle: false
                    });
                }
            }
        }
    });
});

document.body.addEventListener('htmx:afterSwap', (event) => {
    // If the swapped content contains toast elements, initialize them.
    const toastElements = document.querySelectorAll('.toast');
    toastElements.forEach((toastEl) => {
        const toast = new bootstrap.Toast(toastEl);
        toast.show();
    });
});

document.body.addEventListener('htmx:afterSwap', (evt: any) => {
    initializeCharts(evt.detail.target ?? document);
});

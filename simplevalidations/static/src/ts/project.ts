// Import all javascript libraries and functions and css styles.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import * as bootstrap from 'bootstrap';
import * as coreui from '@coreui/coreui';
import { Chart, registerables } from 'chart.js';
import htmx from 'htmx.org';

declare global {
    interface Window {
        bootstrap: typeof bootstrap;
        htmx: typeof htmx;
        Chart: typeof Chart;
        coreui: typeof coreui;
    }
}
window.bootstrap = bootstrap;
window.htmx = htmx;
Chart.register(...registerables);
window.Chart = Chart;
window.coreui = coreui;
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

    initializeThemeToggle();
    setupSidebarToggle();
    initializeMarketingNavbarScroll();

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


type ThemeMode = 'light' | 'dark';

const THEME_STORAGE_KEY = 'simplevalidations:theme-preference';
const THEME_MEDIA_QUERY = '(prefers-color-scheme: dark)';

function applyThemePreference(theme: ThemeMode): void {
    document.documentElement.setAttribute('data-bs-theme', theme);
    document.body.setAttribute('data-bs-theme', theme);
}

function readStoredTheme(): ThemeMode | null {
    try {
        const value = localStorage.getItem(THEME_STORAGE_KEY);
        if (value === 'light' || value === 'dark') {
            return value;
        }
    } catch (error) {
        console.debug('Unable to access saved theme preference', error);
    }
    return null;
}

function persistThemePreference(theme: ThemeMode): void {
    try {
        localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch (error) {
        console.debug('Unable to persist theme preference', error);
    }
}

function updateThemeToggleState(toggle: HTMLButtonElement | null, theme: ThemeMode): void {
    if (!toggle) {
        return;
    }
    toggle.setAttribute('data-theme-state', theme);
    const label = toggle.querySelector<HTMLElement>('[data-theme-label]');
    const lightLabel = toggle.dataset.themeLabelLight || 'Light';
    const darkLabel = toggle.dataset.themeLabelDark || 'Dark';
    if (label) {
        label.textContent = theme === 'dark' ? darkLabel : lightLabel;
    }
    toggle.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
}

function initializeThemeToggle(): void {
    const toggle = document.querySelector<HTMLButtonElement>('[data-theme-toggle]');
    const mediaQuery = window.matchMedia(THEME_MEDIA_QUERY);

    let manualTheme: ThemeMode | null = readStoredTheme();
    let currentTheme: ThemeMode = manualTheme ?? (mediaQuery.matches ? 'dark' : 'light');

    applyThemePreference(currentTheme);
    updateThemeToggleState(toggle, currentTheme);

    toggle?.addEventListener('click', () => {
        currentTheme = currentTheme === 'dark' ? 'light' : 'dark';
        applyThemePreference(currentTheme);
        updateThemeToggleState(toggle, currentTheme);
        persistThemePreference(currentTheme);
        manualTheme = currentTheme;
    });

    mediaQuery.addEventListener('change', (event) => {
        if (manualTheme !== null) {
            return;
        }
        currentTheme = event.matches ? 'dark' : 'light';
        applyThemePreference(currentTheme);
        updateThemeToggleState(toggle, currentTheme);
    });
}

function setupSidebarToggle(): void {
    const sidebar = document.getElementById('app-sidebar');
    const toggler = document.querySelector<HTMLButtonElement>('[data-app-sidebar-toggle]');
    const backdrop = document.querySelector<HTMLElement>('[data-app-sidebar-dismiss]');
    if (!sidebar || !toggler) {
        return;
    }

    const body = document.body;
    const openClass = 'app-sidebar-open';

    const closeSidebar = () => {
        if (!body.classList.contains(openClass)) {
            return;
        }
        body.classList.remove(openClass);
        toggler.setAttribute('aria-expanded', 'false');
    };

    const toggleSidebar = () => {
        const isOpen = body.classList.toggle(openClass);
        toggler.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    };

    toggler.addEventListener('click', (event) => {
        event.preventDefault();
        toggleSidebar();
    });

    backdrop?.addEventListener('click', () => {
        closeSidebar();
    });

    sidebar.querySelectorAll<HTMLElement>('[data-app-sidebar-link]').forEach((link) => {
        link.addEventListener('click', () => {
            if (window.matchMedia('(max-width: 991.98px)').matches) {
                closeSidebar();
            }
        });
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            closeSidebar();
        }
    });

    const handleResize = () => {
        if (window.matchMedia('(min-width: 992px)').matches) {
            body.classList.remove(openClass);
            toggler.setAttribute('aria-expanded', 'false');
        }
    };

    window.addEventListener('resize', handleResize);
}

function initializeMarketingNavbarScroll(): void {
    const navbar = document.getElementById('site-top-nav');
    if (!navbar) {
        return;
    }

    let lastScroll = window.pageYOffset || document.documentElement.scrollTop;

    window.addEventListener('scroll', () => {
        const currentScroll = window.pageYOffset || document.documentElement.scrollTop;

        if (currentScroll > lastScroll + 4) {
            navbar.classList.add('navbar-hidden');
        } else if (currentScroll < lastScroll - 4 || currentScroll <= 0) {
            navbar.classList.remove('navbar-hidden');
        }

        lastScroll = currentScroll <= 0 ? 0 : currentScroll;
    });
}



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

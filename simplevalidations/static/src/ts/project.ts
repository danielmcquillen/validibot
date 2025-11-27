// Import all javascript libraries and functions and css styles.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

import * as bootstrap from 'bootstrap';
import { Chart, registerables } from 'chart.js';
import htmx from 'htmx.org';
import { initAppFeatures } from './app';
import { initTableSorting } from './tableSorting';

type RoleCode = 'OWNER' | 'ADMIN' | 'AUTHOR' | 'EXECUTOR' | 'ANALYTICS_VIEWER' | 'VALIDATION_RESULTS_VIEWER' | 'WORKFLOW_VIEWER';

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

const ROLE_IMPLICATIONS: Record<RoleCode, RoleCode[]> = {
    OWNER: ['OWNER', 'ADMIN', 'AUTHOR', 'EXECUTOR', 'ANALYTICS_VIEWER', 'VALIDATION_RESULTS_VIEWER', 'WORKFLOW_VIEWER'],
    ADMIN: ['AUTHOR', 'EXECUTOR', 'ANALYTICS_VIEWER', 'VALIDATION_RESULTS_VIEWER', 'WORKFLOW_VIEWER'],
    AUTHOR: ['EXECUTOR', 'ANALYTICS_VIEWER', 'VALIDATION_RESULTS_VIEWER', 'WORKFLOW_VIEWER'],
    EXECUTOR: ['WORKFLOW_VIEWER'],
    ANALYTICS_VIEWER: [],
    VALIDATION_RESULTS_VIEWER: [],
    WORKFLOW_VIEWER: [],
};

function expandRoles(selectedCodes: Set<string>): { expanded: Set<string>; implied: Set<string> } {
    const expanded = new Set(selectedCodes);
    const implied = new Set<string>();
    const frontier = [...selectedCodes];
    while (frontier.length) {
        const role = frontier.pop();
        if (!role) {
            continue;
        }
        (ROLE_IMPLICATIONS[role as RoleCode] || []).forEach((grant) => {
            if (!expanded.has(grant)) {
                expanded.add(grant);
                frontier.push(grant);
            }
            implied.add(grant);
        });
    }
    return { expanded, implied };
}

function initRolePicker(container: HTMLElement): void {
    if (!container || container.dataset.svRolePickerInit === 'true') {
        return;
    }
    container.dataset.svRolePickerInit = 'true';
    const fieldName = container.dataset.fieldName || 'roles';
    const checkboxes = Array.from(
        container.querySelectorAll<HTMLInputElement>('input[type="checkbox"][data-role-code]'),
    );

    // Seed user-selected markers from initial state (exclude implied-at-render).
    checkboxes.forEach((cb) => {
        const isImplied = cb.dataset.implied === 'true';
        if (cb.checked && !isImplied) {
            cb.dataset.userSelected = 'true';
        }
    });
    const updateHiddenFields = () => {
        container.querySelectorAll('.role-hidden-field').forEach((el) => el.remove());
        checkboxes.forEach((cb) => {
            if (cb.disabled && cb.checked) {
                const hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = fieldName;
                hidden.value = cb.value;
                hidden.className = 'role-hidden-field';
                container.appendChild(hidden);
            }
        });
    };

    const applyImplications = () => {
        const explicitSelections = new Set(
            checkboxes
                .filter((cb) => cb.checked && cb.dataset.userSelected === 'true')
                .map((cb) => cb.dataset.roleCode || ''),
        );
        const { expanded, implied } = expandRoles(explicitSelections);
        const impliedByAnother = (code: string) =>
            Array.from(explicitSelections).some((sel) => sel !== code && (ROLE_IMPLICATIONS[sel as RoleCode] || []).includes(code as RoleCode));
        console.debug('role-picker: apply', {
            explicit: Array.from(explicitSelections),
            expanded: Array.from(expanded),
            implied: Array.from(implied),
        });

        // Any implied role should no longer be treated as an explicit user choice.
        checkboxes.forEach((cb) => {
            const code = cb.dataset.roleCode || '';
            if (implied.has(code) || impliedByAnother(code)) {
                cb.dataset.userSelected = '';
            }
        });

        checkboxes.forEach((cb) => {
            const code = cb.dataset.roleCode || '';
            const isImplied = implied.has(code) || impliedByAnother(code);
            cb.checked = expanded.has(code);
            cb.disabled = isImplied || code === 'OWNER';
            cb.dataset.implied = isImplied ? 'true' : '';
            const helper = cb.closest('.organization-role-option')?.querySelector<HTMLElement>('.form-text.text-muted');
            if (helper) {
                helper.hidden = !isImplied;
            }
        });
        updateHiddenFields();
    };

    checkboxes.forEach((cb) => {
        cb.addEventListener('change', () => {
            const code = cb.dataset.roleCode;
            cb.dataset.userSelected = cb.checked && cb.dataset.implied !== 'true' ? 'true' : '';
            console.debug('role-picker: change', {
                code,
                checked: cb.checked,
                userSelected: cb.dataset.userSelected,
                impliedFlag: cb.dataset.implied,
            });
            applyImplications();
        });
    });
    applyImplications();
}

function initRolePickers(root: ParentNode | Document = document): void {
    const containers = root.querySelectorAll<HTMLElement>('.organization-role-list');
    containers.forEach((container) => {
        console.debug('role-picker: init container', { id: container.id || null });
        initRolePicker(container);
    });
}

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
    initRolePickers(document);
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
    initRolePickers(root);

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

// Validation-run detail section state.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// Each report section is an independent Bootstrap collapse. New sessions start
// with every section closed; after a user opens or closes one, sessionStorage
// carries that section's choice to other validation runs in the same browser
// session. The state key intentionally excludes the run ID because the user's
// preference is about the kind of information, not a particular run.

export const VALIDATION_RUN_SECTION_STORAGE_PREFIX =
    'validibot:validationRunSection:';

const SECTION_SELECTOR = '[data-validation-run-section]';
const BODY_SELECTOR = '[data-validation-run-section-body]';
const TOGGLE_SELECTOR = '[data-validation-run-section-toggle]';

type SectionState = 'open' | 'closed';

function availableSessionStorage(): Storage | null {
    try {
        return window.sessionStorage;
    } catch (error) {
        console.debug('Unable to access validation run section preferences', error);
        return null;
    }
}

function readState(storage: Storage | null, sectionKey: string): SectionState | null {
    if (!storage) {
        return null;
    }
    try {
        const value = storage.getItem(
            VALIDATION_RUN_SECTION_STORAGE_PREFIX + sectionKey,
        );
        return value === 'open' || value === 'closed' ? value : null;
    } catch (error) {
        console.debug('Unable to read validation run section preference', error);
        return null;
    }
}

function writeState(
    storage: Storage | null,
    sectionKey: string,
    state: SectionState,
): void {
    if (!storage) {
        return;
    }
    try {
        storage.setItem(
            VALIDATION_RUN_SECTION_STORAGE_PREFIX + sectionKey,
            state,
        );
    } catch (error) {
        console.debug('Unable to persist validation run section preference', error);
    }
}

function applyState(
    body: HTMLElement,
    toggle: HTMLElement,
    state: SectionState,
): void {
    const isOpen = state === 'open';
    body.classList.toggle('show', isOpen);
    toggle.classList.toggle('collapsed', !isOpen);
    toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
}

function findSections(root: ParentNode): HTMLElement[] {
    const sections = Array.from(
        root.querySelectorAll<HTMLElement>(SECTION_SELECTOR),
    );
    if (root instanceof HTMLElement && root.matches(SECTION_SELECTOR)) {
        sections.unshift(root);
    }
    return sections;
}

export function initValidationRunSections(
    root: ParentNode = document,
    storage: Storage | null = availableSessionStorage(),
): void {
    findSections(root).forEach((section) => {
        if (section.dataset.validationRunSectionInitialized === 'true') {
            return;
        }

        const sectionKey = section.dataset.validationRunSection;
        const body = section.querySelector<HTMLElement>(BODY_SELECTOR);
        const toggle = section.querySelector<HTMLElement>(TOGGLE_SELECTOR);
        if (!sectionKey || !body || !toggle) {
            return;
        }

        section.dataset.validationRunSectionInitialized = 'true';
        applyState(body, toggle, readState(storage, sectionKey) ?? 'closed');

        // Bootstrap emits these events as soon as a valid transition begins,
        // so navigation during the short animation still preserves the choice.
        body.addEventListener('show.bs.collapse', () => {
            writeState(storage, sectionKey, 'open');
        });
        body.addEventListener('hide.bs.collapse', () => {
            writeState(storage, sectionKey, 'closed');
        });
    });
}

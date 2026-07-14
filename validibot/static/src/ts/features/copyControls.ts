import { copyTextToClipboard } from '../shared/clipboard';

type CopyText = (value: string) => Promise<boolean>;
type TimeoutScheduler = (handler: () => void, timeout: number) => number;

export interface CopyControlsOptions {
    root?: Document | HTMLElement;
    copyText?: CopyText;
    setTimeout?: TimeoutScheduler;
}

const DEFAULT_COPIED_LABEL = 'Copied';
const DEFAULT_TIMEOUT_MS = 1600;
const initializedRoots = new WeakSet<Document | HTMLElement>();

function readFieldValue(field: Element): string {
    const value = (field as HTMLInputElement | HTMLTextAreaElement).value;
    if (typeof value === 'string') {
        return value;
    }
    return field.textContent ?? '';
}

function parseTimeout(value: string | null): number {
    const timeout = Number.parseInt(value || '', 10);
    return Number.isNaN(timeout) ? DEFAULT_TIMEOUT_MS : timeout;
}

async function handleCopyClick(
    event: Event,
    root: Document | HTMLElement,
    copyText: CopyText,
    scheduleTimeout: TimeoutScheduler,
): Promise<void> {
    const target = event.target;
    if (!(target instanceof Element)) {
        return;
    }

    const trigger = target.closest<HTMLElement>('[data-copy-target]');
    if (!trigger) {
        return;
    }

    const selector = trigger.dataset.copyTarget;
    if (!selector) {
        return;
    }

    const field = root.querySelector(selector);
    if (!field) {
        return;
    }

    const value = readFieldValue(field);
    if (!value.trim()) {
        return;
    }

    if (!trigger.dataset.defaultLabel) {
        trigger.dataset.defaultLabel = trigger.textContent?.trim() || '';
    }

    let success = false;
    try {
        success = await copyText(value);
    } catch {
        success = false;
    }
    if (!success) {
        return;
    }

    trigger.textContent = trigger.dataset.copiedLabel || DEFAULT_COPIED_LABEL;
    scheduleTimeout(() => {
        trigger.textContent = trigger.dataset.defaultLabel || '';
    }, parseTimeout(trigger.dataset.copyTimeout || null));
}

export function initCopyControls(options: CopyControlsOptions = {}): void {
    const root = options.root ?? document;
    if (initializedRoots.has(root)) {
        return;
    }
    initializedRoots.add(root);

    const copyText = options.copyText ?? copyTextToClipboard;
    const scheduleTimeout = options.setTimeout ?? window.setTimeout.bind(window);

    root.addEventListener('click', (event) => {
        void handleCopyClick(event, root, copyText, scheduleTimeout);
    });
}

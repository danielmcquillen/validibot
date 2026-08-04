/** Accessible, idempotent Bootstrap tooltips backed by rich template content. */

type TooltipInstance = {
    dispose: () => void;
    hide: () => void;
};

type TooltipConstructor = {
    new (element: HTMLElement): TooltipInstance;
    getInstance: (element: Element) => TooltipInstance | null;
};

const TOOLTIP_SELECTOR = '[data-bs-toggle="tooltip"]';
const CONTENT_SELECTOR = 'template.rich-tooltip-content, template.cel-tooltip-content';

function matchingElements(root: ParentNode, selector: string): HTMLElement[] {
    const elements = Array.from(root.querySelectorAll<HTMLElement>(selector));
    if (root instanceof HTMLElement && root.matches(selector)) {
        elements.unshift(root);
    }
    return elements;
}

export function initRichTooltips(
    root: ParentNode,
    Tooltip: TooltipConstructor,
): void {
    /** Initialize each tooltip once and load HTML from its sibling template. */

    matchingElements(root, TOOLTIP_SELECTOR).forEach((element) => {
        const content = element.parentElement?.querySelector<HTMLTemplateElement>(
            CONTENT_SELECTOR,
        );
        if (content) {
            element.setAttribute('title', content.innerHTML.trim());
        }
        if (!Tooltip.getInstance(element)) {
            new Tooltip(element);
        }
    });
}

export function disposeRichTooltips(
    root: ParentNode,
    Tooltip: TooltipConstructor,
): void {
    /** Dispose instances before HTMX removes their trigger elements. */

    matchingElements(root, TOOLTIP_SELECTOR).forEach((element) => {
        Tooltip.getInstance(element)?.dispose();
    });
}

export function hideRichTooltips(
    root: ParentNode,
    Tooltip: TooltipConstructor,
): void {
    /** Hide open overlays while preserving instances outside an HTMX swap. */

    matchingElements(root, TOOLTIP_SELECTOR).forEach((element) => {
        Tooltip.getInstance(element)?.hide();
    });
}

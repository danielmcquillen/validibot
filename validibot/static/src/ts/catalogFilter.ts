/*
 * Catalog entry filtering utilities.
 * Provides local filtering and highlighting for catalog cards.
 */

type CatalogEntryElement = HTMLElement & {
  dataset: DOMStringMap & {
    svCatalogLabel?: string;
    svCatalogSlug?: string;
  };
};

function escapeRegExp(input: string): string {
  return input.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function highlightElement(element: HTMLElement | null, query: string): void {
  if (!element) {
    return;
  }
  const original = element.dataset.originalText ?? element.textContent ?? '';
  if (!element.dataset.originalText) {
    element.dataset.originalText = original;
  }
  if (!query) {
    element.textContent = original;
    return;
  }
  const regex = new RegExp(`(${escapeRegExp(query)})`, 'ig');
  const parts = original.split(regex);
  element.textContent = '';
  parts.forEach((part) => {
    if (part.match(regex)) {
      const mark = document.createElement('mark');
      mark.textContent = part;
      element.appendChild(mark);
    } else {
      element.appendChild(document.createTextNode(part));
    }
  });
}

function filterEntries(
  query: string,
  entries: CatalogEntryElement[],
  emptyMessage?: HTMLElement,
): void {
  const normalized = query.trim().toLowerCase();
  let visibleCount = 0;
  entries.forEach((entry) => {
    const haystack = `${entry.dataset.svCatalogLabel ?? ''} ${entry.dataset.svCatalogSlug ?? ''}`.toLowerCase();
    const matches = normalized ? haystack.includes(normalized) : true;
    entry.classList.toggle('d-none', !matches);
    const labelEl = entry.querySelector<HTMLElement>('.catalog-entry-label');
    const slugEl = entry.querySelector<HTMLElement>('.catalog-entry-slug');
    if (matches) {
      visibleCount += 1;
      highlightElement(labelEl, normalized);
      highlightElement(slugEl, normalized);
    } else {
      highlightElement(labelEl, '');
      highlightElement(slugEl, '');
    }
  });
  if (emptyMessage) {
    emptyMessage.classList.toggle('d-none', visibleCount > 0);
  }
}

export function initCatalogFilters(root: ParentNode | Document = document): void {
  const catalogs = root.querySelectorAll<HTMLElement>('[data-sv-catalog]');
  catalogs.forEach((catalog) => {
    const filters = catalog.querySelectorAll<HTMLInputElement>('[data-sv-catalog-filter]');
    filters.forEach((filterInput) => {
      const targetSelector = filterInput.dataset.svCatalogFilterTarget;
      const targetPanel = targetSelector
        ? catalog.querySelector<HTMLElement>(targetSelector)
        : catalog;
      if (!targetPanel) {
        return;
      }
      const entries = Array.from(
        targetPanel.querySelectorAll<CatalogEntryElement>('[data-sv-catalog-entry]'),
      );
      if (!entries.length) {
        filterInput.disabled = true;
        return;
      }
      const emptyMessage = targetPanel.querySelector<HTMLElement>('[data-sv-catalog-empty]');
      filterInput.addEventListener('input', () => {
        filterEntries(filterInput.value, entries, emptyMessage ?? undefined);
      });
    });
  });
}

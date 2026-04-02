/**
 * Signal mapping sample data table — checkbox selection, select-all,
 * and bulk-add functionality.
 *
 * Initialised by `initSignalMapping()` which is called from `app.ts`
 * on both DOMContentLoaded and htmx:onLoad (so it re-binds after
 * HTMx swaps the sample data results partial).
 */

/**
 * Update the select-all checkbox to reflect the current selection state.
 *
 * Uses the indeterminate property — the standard HTML tri-state for
 * "some but not all" — which Bootstrap renders as a dash icon.
 */
function syncSelectAll(
  selectAll: HTMLInputElement,
  checkboxes: HTMLInputElement[],
): void {
  const total = checkboxes.length;
  const checked = checkboxes.filter((cb) => cb.checked).length;

  selectAll.checked = total > 0 && checked === total;
  selectAll.indeterminate = checked > 0 && checked < total;
}

/** Set the bulk button text label (safe DOM API, no icon). */
function setBulkButtonLabel(btn: HTMLButtonElement, text: string): void {
  btn.textContent = text;
}

/** Set the bulk button to a loading spinner state (safe DOM API). */
function setBulkButtonLoading(btn: HTMLButtonElement): void {
  while (btn.firstChild) {
    btn.removeChild(btn.firstChild);
  }
  const spinner = document.createElement('span');
  spinner.className = 'spinner-border spinner-border-sm me-1';
  spinner.setAttribute('role', 'status');
  btn.appendChild(spinner);
  btn.appendChild(document.createTextNode('Adding...'));
}

function syncBulkButton(
  bulkBtn: HTMLButtonElement,
  checkboxes: HTMLInputElement[],
): void {
  const checked = checkboxes.filter((cb) => cb.checked).length;

  if (checked > 1) {
    bulkBtn.style.display = '';
    bulkBtn.disabled = false;
    setBulkButtonLabel(bulkBtn, `Bulk Add (${checked})`);
  } else {
    bulkBtn.style.display = 'none';
  }
}

function initSampleDataTable(): void {
  // Always query from document rather than a scoped root.  After an
  // HTMx innerHTML swap into #sample-data-results, onLoad fires once
  // per top-level child node.  The bulk button and the table are
  // siblings, so querying from one child's root can't find the other.
  const table = document.querySelector<HTMLTableElement>('#sample-data-table');
  const bulkBtn = document.querySelector<HTMLButtonElement>('#bulk-add-btn');
  const selectAll = document.querySelector<HTMLInputElement>(
    '#select-all-candidates',
  );

  if (!table || !bulkBtn || !selectAll) {
    return;
  }

  // Prevent double-init after HTMx swaps
  if (table.dataset.signalMappingInit === 'true') {
    return;
  }
  table.dataset.signalMappingInit = 'true';

  const checkboxes = Array.from(
    table.querySelectorAll<HTMLInputElement>('.candidate-checkbox'),
  );

  if (checkboxes.length === 0) {
    // All candidates are already added — hide the select-all and bulk button
    selectAll.style.display = 'none';
    bulkBtn.style.display = 'none';
    return;
  }

  // Select-all toggle
  selectAll.addEventListener('change', () => {
    checkboxes.forEach((cb) => {
      cb.checked = selectAll.checked;
    });
    syncBulkButton(bulkBtn, checkboxes);
  });

  // Individual checkbox changes
  checkboxes.forEach((cb) => {
    cb.addEventListener('change', () => {
      syncSelectAll(selectAll, checkboxes);
      syncBulkButton(bulkBtn, checkboxes);
    });
  });

  // Bulk add click — uses htmx.ajax() so CSRF is handled by the
  // global hx-headers on <body>, matching the project's standard pattern.
  bulkBtn.addEventListener('click', () => {
    const selected = checkboxes
      .filter((cb) => cb.checked)
      .map((cb) => {
        const row = cb.closest('tr')!;
        return {
          name: row.dataset.candidateName || '',
          source_path: row.dataset.candidatePath || '',
        };
      });

    if (selected.length === 0) {
      return;
    }

    bulkBtn.disabled = true;
    setBulkButtonLoading(bulkBtn);

    const url = bulkBtn.dataset.bulkAddUrl || '';
    window.htmx.ajax('post', url, {
      values: { candidates: JSON.stringify(selected) },
      swap: 'none',
    });
  });
}

/**
 * Re-submit the sample data form when signals change (add/delete/edit)
 * so the "Added" badges refresh.  Only fires if the textarea has content
 * (no point re-parsing if nothing was pasted yet).
 */
function listenForSignalsChanged(): void {
  document.body.addEventListener('signals-changed', () => {
    const form = document.querySelector<HTMLFormElement>('#sample-data-form');
    const textarea = form?.querySelector<HTMLTextAreaElement>(
      'textarea[name="sample_data"]',
    );
    if (form && textarea && textarea.value.trim()) {
      window.htmx.trigger(form, 'submit');
    }
  });
}

let signalsListenerInstalled = false;

export function initSignalMapping(
  _root: ParentNode | Document = document,
): void {
  initSampleDataTable();
  if (!signalsListenerInstalled) {
    signalsListenerInstalled = true;
    listenForSignalsChanged();
  }
}

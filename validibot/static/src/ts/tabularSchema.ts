const EDITOR_SELECTOR = '[data-tabular-column-editor]';
const ROW_SELECTOR = '[data-tabular-column-row]';

function activeRows(editor: HTMLElement): HTMLElement[] {
  return Array.from(editor.querySelectorAll<HTMLElement>(ROW_SELECTOR)).filter(
    (row) => !row.hidden,
  );
}

function syncConstraintVisibility(row: HTMLElement): void {
  const typeField = row.querySelector<HTMLSelectElement>('select[name$="-type"]');
  if (!typeField) {
    return;
  }
  const numeric = typeField.value === 'number' || typeField.value === 'integer';
  const string = typeField.value === 'string';

  row.querySelectorAll<HTMLElement>('[data-tabular-constraint]').forEach((group) => {
    const constraint = group.dataset.tabularConstraint;
    const visible =
      (constraint === 'numeric' && numeric) ||
      (constraint === 'string' && string);
    group.classList.toggle('d-none', !visible);
    group.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(
      'input, select, textarea',
    ).forEach((field) => {
      field.disabled = !visible;
    });
  });
}

function syncPrimaryKeyRequirement(row: HTMLElement): void {
  const primaryKey = row.querySelector<HTMLInputElement>(
    'input[name$="-primary_key"]',
  );
  const required = row.querySelector<HTMLInputElement>(
    'input[name$="-required"]',
  );
  if (!primaryKey || !required) {
    return;
  }
  if (primaryKey.checked) {
    required.checked = true;
  }
  required.disabled = primaryKey.checked;

  // Explain the lock: when Required is forced on by a primary key, swap the
  // wrapper's tooltip to say so; otherwise restore the field's help text.
  const wrapper = required.closest<HTMLElement>(
    '.tabular-column-card__required',
  );
  if (wrapper) {
    wrapper.title = primaryKey.checked
      ? wrapper.dataset.requiredLockedTitle ?? ''
      : wrapper.dataset.requiredDefaultTitle ?? '';
  }
}

function syncRequiredWhenOptions(editor: HTMLElement): void {
  const rows = activeRows(editor);
  const names = rows
    .map((row) =>
      row.querySelector<HTMLInputElement>('input[name$="-name"]')?.value.trim() ?? '',
    )
    .filter(Boolean);

  rows.forEach((row) => {
    const select = row.querySelector<HTMLSelectElement>(
      'select[name$="-required_when_present"]',
    );
    const name = row.querySelector<HTMLInputElement>(
      'input[name$="-name"]',
    )?.value.trim();
    const required = row.querySelector<HTMLInputElement>(
      'input[name$="-required"]',
    );
    const primaryKey = row.querySelector<HTMLInputElement>(
      'input[name$="-primary_key"]',
    );
    if (!select) {
      return;
    }

    const selected = select.value;
    const emptyLabel = select.options[0]?.text ?? 'Never (optional column)';
    select.replaceChildren(new Option(emptyLabel, ''));
    names
      .filter((candidate) => candidate !== name)
      .forEach((candidate) => select.add(new Option(candidate, candidate)));
    select.value = names.includes(selected) && selected !== name ? selected : '';
    select.disabled = Boolean(required?.checked || primaryKey?.checked);
    if (select.disabled) {
      select.value = '';
    }
  });
}

function syncEditor(editor: HTMLElement): void {
  const rows = Array.from(editor.querySelectorAll<HTMLElement>(ROW_SELECTOR));
  rows.forEach((row) => {
    const deleteField = row.querySelector<HTMLInputElement>(
      'input[name$="-DELETE"]',
    );
    row.hidden = Boolean(deleteField?.checked);
    syncConstraintVisibility(row);
    syncPrimaryKeyRequirement(row);
  });

  const currentRows = activeRows(editor);
  syncRequiredWhenOptions(editor);
  const count = editor
    .closest<HTMLElement>('#tabular-schema-workspace')
    ?.querySelector<HTMLElement>('[data-tabular-column-count]');
  if (count) {
    const label =
      currentRows.length === 1
        ? count.dataset.singularLabel ?? 'column'
        : count.dataset.pluralLabel ?? 'columns';
    count.textContent = `${currentRows.length} ${label}`;
  }

  currentRows.forEach((row) => {
    const orderField = row.querySelector<HTMLInputElement>('input[name$="-ORDER"]');
    if (orderField) {
      orderField.value = String(currentRows.indexOf(row) + 1);
    }
    const removeButton = row.querySelector<HTMLButtonElement>(
      '[data-tabular-remove-column]',
    );
    if (removeButton) {
      removeButton.disabled = currentRows.length === 1;
      removeButton.title =
        currentRows.length === 1
          ? editor.dataset.minimumColumnMessage ?? ''
          : '';
    }
    const upButton = row.querySelector<HTMLButtonElement>(
      '[data-tabular-move-column="up"]',
    );
    const downButton = row.querySelector<HTMLButtonElement>(
      '[data-tabular-move-column="down"]',
    );
    const rowIndex = currentRows.indexOf(row);
    if (upButton) {
      upButton.disabled = rowIndex === 0;
    }
    if (downButton) {
      downButton.disabled = rowIndex === currentRows.length - 1;
    }
  });
}

function moveRow(editor: HTMLElement, row: HTMLElement, direction: 'up' | 'down'): void {
  const rows = activeRows(editor);
  const index = rows.indexOf(row);
  const sibling = direction === 'up' ? rows[index - 1] : rows[index + 1];
  if (!sibling) {
    return;
  }
  if (direction === 'up') {
    sibling.before(row);
  } else {
    sibling.after(row);
  }
  syncEditor(editor);
  row.querySelector<HTMLButtonElement>(
    `[data-tabular-move-column="${direction}"]`,
  )?.focus();
}

function bindEditor(editor: HTMLElement): void {
  if (editor.dataset.tabularEditorInitialized === 'true') {
    syncEditor(editor);
    return;
  }
  editor.dataset.tabularEditorInitialized = 'true';

  editor.addEventListener('change', (event) => {
    const target = event.target;
    if (target instanceof HTMLSelectElement && target.name.endsWith('-type')) {
      const row = target.closest<HTMLElement>(ROW_SELECTOR);
      if (row) {
        syncConstraintVisibility(row);
      }
    }
    if (
      target instanceof HTMLInputElement &&
      (target.name.endsWith('-primary_key') || target.name.endsWith('-required'))
    ) {
      const row = target.closest<HTMLElement>(ROW_SELECTOR);
      if (row) {
        syncPrimaryKeyRequirement(row);
        syncRequiredWhenOptions(editor);
      }
    }
  });
  editor.addEventListener('input', (event) => {
    const target = event.target;
    if (target instanceof HTMLInputElement && target.name.endsWith('-name')) {
      syncRequiredWhenOptions(editor);
    }
  });

  editor.addEventListener('click', (event) => {
    const target = event.target;
    if (!(target instanceof Element)) {
      return;
    }
    const removeButton = target.closest<HTMLButtonElement>(
      '[data-tabular-remove-column]',
    );
    const moveButton = target.closest<HTMLButtonElement>(
      '[data-tabular-move-column]',
    );
    if (moveButton) {
      const row = moveButton.closest<HTMLElement>(ROW_SELECTOR);
      const direction = moveButton.dataset.tabularMoveColumn;
      if (row && (direction === 'up' || direction === 'down')) {
        moveRow(editor, row, direction);
      }
      return;
    }
    if (!removeButton) {
      return;
    }
    const row = removeButton.closest<HTMLElement>(ROW_SELECTOR);
    const deleteField = row?.querySelector<HTMLInputElement>(
      'input[name$="-DELETE"]',
    );
    if (row && deleteField && activeRows(editor).length > 1) {
      deleteField.checked = true;
      row.hidden = true;
      syncEditor(editor);
    }
  });

  syncEditor(editor);
}

export function initTabularSchemas(
  root: ParentNode | Document = document,
): void {
  const editors = new Set<HTMLElement>();
  if (root instanceof Element) {
    const closestEditor = root.closest<HTMLElement>(EDITOR_SELECTOR);
    if (closestEditor) {
      editors.add(closestEditor);
    }
    if (root.matches(EDITOR_SELECTOR)) {
      editors.add(root as HTMLElement);
    }
  }
  root.querySelectorAll<HTMLElement>(EDITOR_SELECTOR).forEach((editor) => {
    editors.add(editor);
  });
  editors.forEach(bindEditor);
  if (root instanceof Element && root.matches('[data-tabular-new-row]')) {
    root.removeAttribute('data-tabular-new-row');
    root.querySelector<HTMLInputElement>('input[name$="-name"]')?.focus();
  }
}

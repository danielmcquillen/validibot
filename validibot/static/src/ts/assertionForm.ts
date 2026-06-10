/*
 * Assertion form controller.
 * Handles conditional field visibility and initial focus for the target field.
 */

const FIELD_WRAPPER_SELECTORS = '.form-group, .form-check, .mb-3, .form-floating';

const CONDITIONAL_FIELDS = [
  'operator',
  'comparison_value',
  'comparison_value_secondary',
  'list_values',
  'regex_pattern',
  'include_min',
  'include_max',
  'case_insensitive',
  'unicode_fold',
  'coerce_types',
  'treat_missing_as_null',
  'tolerance_value',
  'tolerance_mode',
  'datetime_value',
  'collection_operator',
  'collection_value',
  'cel_expression',
  'when_expression',
  'shacl_description',
  'shacl_target_graph',
  'shacl_query',
] as const;

type ConditionalFieldName = (typeof CONDITIONAL_FIELDS)[number];

const GENERAL_BASIC_FIELDS: ConditionalFieldName[] = [
  'coerce_types',
  'treat_missing_as_null',
  'when_expression',
];
const STRING_OPTION_FIELDS: ConditionalFieldName[] = ['case_insensitive', 'unicode_fold'];

const OPERATOR_FIELDS: Record<string, readonly ConditionalFieldName[]> = {
  eq: ['comparison_value'],
  ne: ['comparison_value'],
  lt: ['comparison_value'],
  le: ['comparison_value'],
  gt: ['comparison_value'],
  ge: ['comparison_value'],
  len_eq: ['comparison_value'],
  len_le: ['comparison_value'],
  len_ge: ['comparison_value'],
  type_is: ['comparison_value'],
  between: ['comparison_value', 'comparison_value_secondary', 'include_min', 'include_max'],
  count_between: ['comparison_value', 'comparison_value_secondary', 'include_min', 'include_max'],
  in: ['list_values'],
  not_in: ['list_values'],
  subset: ['list_values'],
  superset: ['list_values'],
  unique: [],
  contains: ['comparison_value'],
  not_contains: ['comparison_value'],
  starts_with: ['comparison_value'],
  ends_with: ['comparison_value'],
  matches: ['regex_pattern'],
  is_null: [],
  not_null: [],
  is_empty: [],
  not_empty: [],
  approx_eq: ['comparison_value', 'tolerance_value', 'tolerance_mode'],
  before: ['datetime_value'],
  after: ['datetime_value'],
  within: ['comparison_value'],
  any: ['collection_operator', 'collection_value'],
  all: ['collection_operator', 'collection_value'],
  none: ['collection_operator', 'collection_value'],
};

const STRING_OPERATORS = new Set<string>([
  'contains',
  'not_contains',
  'starts_with',
  'ends_with',
  'matches',
]);

type CelAssistColumn = {
  name: string;
  type: string;
  alias?: string;
};

type CelAssistData = {
  stage: 'dataset' | 'row' | 'column';
  columns: CelAssistColumn[];
  catalog: Array<{ value: string; label: string }>;
};

type CelSuggestion = {
  label: string;
  insert: string;
  detail: string;
};

class AssertionFormController {
  private wrappers = new Map<ConditionalFieldName, HTMLElement>();
  private typeField: HTMLSelectElement | null;
  private operatorField: HTMLSelectElement | null;
  private targetField: HTMLElement | null;
  private targetCatalogField: HTMLElement | null;
  private targetPathInput: HTMLInputElement | null;
  private targetCatalogInput: HTMLInputElement | null;
  private shaclQueryField: HTMLTextAreaElement | null;
  private shaclFileInputInjected = false;
  private celAssistInitialized = false;
  private focusApplied = false;

  constructor(private form: HTMLFormElement) {
    this.typeField = this.form.querySelector<HTMLSelectElement>('#id_assertion_type');
    this.operatorField = this.form.querySelector<HTMLSelectElement>('#id_operator');
    this.targetPathInput = this.form.querySelector<HTMLInputElement>('[name="target_data_path"]');
    this.targetCatalogInput = this.form.querySelector<HTMLInputElement>('[name="target_catalog_entry"]');
    this.targetField = this.targetPathInput?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? null;
    this.targetCatalogField = this.targetCatalogInput?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? null;
    this.shaclQueryField = this.form.querySelector<HTMLTextAreaElement>('#id_shacl_query');
  }

  init(): void {
    if (!this.typeField || !this.operatorField) {
      return;
    }
    this.bindEvents();
    this.injectShaclFileUpload();
    this.initCelAssist();
    this.refresh();
  }

  refresh(): void {
    if (!this.typeField || !this.operatorField) {
      return;
    }
    this.collectWrappers();
    this.updateVisibility();
    this.focusTargetField();
  }

  private bindEvents(): void {
    if (!this.typeField || !this.operatorField) {
      return;
    }
    this.typeField.addEventListener('change', () => {
      if (this.typeField?.value === 'cel_expr' && this.operatorField) {
        this.operatorField.value = '';
      }
      this.updateVisibility();
    });
    this.operatorField.addEventListener('change', () => this.updateVisibility());

    // When the user modifies the visible target field, clear the hidden
    // catalog entry field so the backend resolves from the new text value
    // instead of the stale pre-populated hidden value.
    if (this.targetPathInput && this.targetCatalogInput) {
      this.targetPathInput.addEventListener('input', () => {
        if (this.targetCatalogInput) {
          this.targetCatalogInput.value = '';
        }
      });
    }
  }

  private collectWrappers(): void {
    this.wrappers.clear();
    CONDITIONAL_FIELDS.forEach((name) => {
      const field = this.form.querySelector<HTMLElement>(`[name="${name}"]`);
      if (!field) {
        return;
      }
      const wrapper =
        field.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? (field.parentElement as HTMLElement | null);
      if (wrapper) {
        this.wrappers.set(name, wrapper);
      }
    });
  }

  private hideAllConditional(): void {
    CONDITIONAL_FIELDS.forEach((name) => this.setVisible(name, false));
  }

  private showFields(names: Iterable<ConditionalFieldName>): void {
    Array.from(names).forEach((name) => this.setVisible(name, true));
  }

  private setVisible(name: ConditionalFieldName, visible: boolean): void {
    const wrapper = this.wrappers.get(name);
    if (!wrapper) {
      return;
    }
    wrapper.style.display = visible ? '' : 'none';
  }

  private updateVisibility(): void {
    if (!this.typeField || !this.operatorField) {
      return;
    }
    this.hideAllConditional();
    const typeValue = this.typeField.value;
    if (typeValue === 'shacl') {
      this.showFields(['shacl_description', 'shacl_target_graph', 'shacl_query']);
      this.operatorField.value = '';
      this.toggleTargetVisibility(false);
      return;
    }
    if (typeValue === 'cel_expr') {
      this.showFields(['cel_expression', 'when_expression']);
      this.operatorField.value = '';
      this.toggleTargetVisibility(false);
      return;
    }
    this.toggleTargetVisibility(true);

    this.setVisible('operator', true);
    const operatorValue = this.operatorField.value;
    if (!operatorValue) {
      return;
    }

    const visible = new Set<ConditionalFieldName>(GENERAL_BASIC_FIELDS);
    (OPERATOR_FIELDS[operatorValue] ?? []).forEach((name) => visible.add(name));
    if (STRING_OPERATORS.has(operatorValue)) {
      STRING_OPTION_FIELDS.forEach((name) => visible.add(name));
    }
    this.showFields(visible);
  }

  /**
   * Insert a small "Load from file" affordance above the SPARQL textarea
   * so authors can pick a .sparql or .rq file from disk and have its
   * contents loaded into the textarea. No server round-trip: the file is
   * read locally via FileReader and the existing save-time scrubber +
   * length cap run on the loaded text exactly as if it had been typed.
   *
   * Injected once per form instance. Subsequent refresh() calls leave
   * the existing input in place (idempotent via shaclFileInputInjected).
   *
   * Why JS injection rather than a Django form field: the load button
   * never carries a value to the server, so making it a Field on the
   * Django form would add a phantom field and force the crispy layout
   * to manage something it shouldn't. Injecting it in the controller
   * keeps both the UI and the JS that handles it in one file.
   */
  private injectShaclFileUpload(): void {
    if (this.shaclFileInputInjected || !this.shaclQueryField) {
      return;
    }
    const textarea = this.shaclQueryField;
    const wrapper = textarea.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS);
    if (!wrapper) {
      return;
    }

    const container = document.createElement('div');
    container.className = 'd-flex align-items-center gap-2 mb-2 small text-muted';

    const label = document.createElement('label');
    label.className = 'btn btn-sm btn-outline-secondary mb-0';
    label.textContent = 'Load from file…';
    label.style.cursor = 'pointer';

    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.sparql,.rq,application/sparql-query,text/plain';
    input.className = 'visually-hidden';
    input.setAttribute('aria-label', 'Load a SPARQL query from a file');
    label.appendChild(input);

    const hint = document.createElement('span');
    hint.textContent = 'Accepts .sparql or .rq files';

    const status = document.createElement('span');
    status.className = 'ms-auto small fst-italic';
    status.style.minHeight = '1em';

    container.appendChild(label);
    container.appendChild(hint);
    container.appendChild(status);

    input.addEventListener('change', () => {
      const file = input.files?.[0];
      if (!file) {
        return;
      }
      // Cap the read at the textarea's own maxlength when available, so
      // we don't load a 10 MB file just to have the form scrubber
      // reject it. Falls back to 50,000 chars (the engine's hard cap).
      const maxLength = textarea.maxLength > 0 ? textarea.maxLength : 50_000;
      if (file.size > maxLength * 2) {
        status.textContent = `${file.name} is too large to load (cap ~${maxLength} chars).`;
        status.classList.add('text-danger');
        input.value = '';
        return;
      }
      const reader = new FileReader();
      reader.addEventListener('load', () => {
        const result = reader.result;
        if (typeof result !== 'string') {
          return;
        }
        textarea.value = result;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        status.textContent = `Loaded ${file.name} (${result.length.toLocaleString()} chars).`;
        status.classList.remove('text-danger');
        // Allow re-selecting the same file later (otherwise the change
        // event won't fire on a second pick of the same filename).
        input.value = '';
      });
      reader.addEventListener('error', () => {
        status.textContent = `Could not read ${file.name}.`;
        status.classList.add('text-danger');
      });
      reader.readAsText(file);
    });

    wrapper.insertBefore(container, textarea);
    this.shaclFileInputInjected = true;
  }

  private initCelAssist(): void {
    if (this.celAssistInitialized) {
      return;
    }
    const textarea = this.form.querySelector<HTMLTextAreaElement>('#id_cel_expression');
    const dataElement = this.form.querySelector<HTMLScriptElement>(
      '#tabular-cel-assist-data',
    );
    const wrapper = textarea?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS);
    if (!textarea || !dataElement || !wrapper) {
      return;
    }

    let data: CelAssistData;
    try {
      data = JSON.parse(dataElement.textContent ?? '') as CelAssistData;
    } catch {
      return;
    }

    const suggestions = this.buildCelSuggestions(data);
    const panel = document.createElement('div');
    panel.className = 'border rounded bg-body-tertiary mt-2 p-2';
    panel.setAttribute('data-cel-assist-panel', '');

    const heading = document.createElement('div');
    heading.className = 'd-flex flex-wrap align-items-center gap-2 small mb-2';
    const title = document.createElement('span');
    title.className = 'fw-semibold';
    title.textContent = 'CEL suggestions';
    const hint = document.createElement('span');
    hint.className = 'text-muted';
    hint.textContent = 'Type to filter, or press Ctrl+Space.';
    heading.append(title, hint);

    const namespaces = document.createElement('div');
    namespaces.className = 'd-flex flex-wrap gap-1 mb-2';
    const namespaceLabels =
      data.stage === 'dataset'
        ? ['i.* dataset', 's.* signals', 'submission.*']
        : data.stage === 'row'
          ? ['row.* current row', 'i.* dataset', 's.* signals']
          : ['col.* aggregates', 'i.* dataset', 's.* signals'];
    namespaceLabels.forEach((label) => {
      const badge = document.createElement('span');
      badge.className = 'badge text-bg-light border';
      badge.textContent = label;
      namespaces.appendChild(badge);
    });

    const list = document.createElement('div');
    list.className = 'list-group';
    list.setAttribute('role', 'listbox');
    list.hidden = true;
    panel.append(heading, namespaces, list);
    wrapper.appendChild(panel);

    let visible: CelSuggestion[] = [];
    let activeIndex = 0;

    const currentQuery = (): { text: string; start: number; end: number } => {
      const end = textarea.selectionStart ?? textarea.value.length;
      const prefix = textarea.value.slice(0, end);
      const match = prefix.match(/[A-Za-z0-9_.\[\]"':-]*$/);
      const text = match?.[0] ?? '';
      return { text, start: end - text.length, end };
    };

    const insertSuggestion = (suggestion: CelSuggestion): void => {
      const query = currentQuery();
      textarea.setRangeText(suggestion.insert, query.start, query.end, 'end');
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      list.hidden = true;
      textarea.focus();
    };

    const render = (force = false): void => {
      const query = currentQuery().text.toLowerCase();
      if (!force && !query) {
        list.hidden = true;
        return;
      }
      visible = suggestions
        .filter((suggestion) => {
          const haystack = `${suggestion.label} ${suggestion.detail}`.toLowerCase();
          return !query || haystack.includes(query);
        })
        .slice(0, 10);
      activeIndex = Math.min(activeIndex, Math.max(visible.length - 1, 0));
      list.replaceChildren();
      visible.forEach((suggestion, index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className =
          'list-group-item list-group-item-action d-flex justify-content-between gap-3 py-2';
        button.setAttribute('role', 'option');
        button.setAttribute('aria-selected', String(index === activeIndex));
        if (index === activeIndex) {
          button.classList.add('active');
        }
        const label = document.createElement('code');
        label.textContent = suggestion.label;
        const detail = document.createElement('span');
        detail.className = 'small opacity-75 text-end';
        detail.textContent = suggestion.detail;
        button.append(label, detail);
        button.addEventListener('mousedown', (event) => event.preventDefault());
        button.addEventListener('click', () => insertSuggestion(suggestion));
        list.appendChild(button);
      });
      list.hidden = visible.length === 0;
    };

    textarea.addEventListener('input', () => render());
    textarea.addEventListener('focus', () => render(false));
    textarea.addEventListener('keydown', (event) => {
      if (event.ctrlKey && event.code === 'Space') {
        event.preventDefault();
        render(true);
        return;
      }
      if (list.hidden || visible.length === 0) {
        return;
      }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        activeIndex =
          event.key === 'ArrowDown'
            ? (activeIndex + 1) % visible.length
            : (activeIndex - 1 + visible.length) % visible.length;
        render(true);
      } else if (event.key === 'Tab' && visible[activeIndex]) {
        event.preventDefault();
        insertSuggestion(visible[activeIndex]);
      } else if (event.key === 'Escape') {
        list.hidden = true;
      }
    });
    this.celAssistInitialized = true;
  }

  private buildCelSuggestions(data: CelAssistData): CelSuggestion[] {
    const suggestions: CelSuggestion[] = data.catalog.map((entry) => ({
      label: entry.value,
      insert: entry.value,
      detail: entry.label,
    }));

    if (data.stage === 'row') {
      data.columns.forEach((column) => {
        const canonical = `row[${JSON.stringify(column.name)}]`;
        suggestions.push({
          label: column.alias ? `row.${column.alias}` : canonical,
          insert: canonical,
          detail: `${column.type} column`,
        });
      });
    } else if (data.stage === 'column') {
      const commonAggregates = [
        'distinct_count',
        'null_count',
        'non_null_count',
        'null_ratio',
        'min',
        'max',
      ];
      data.columns.forEach((column) => {
        const aggregates =
          column.type === 'number' || column.type === 'integer'
            ? [...commonAggregates, 'sum']
            : commonAggregates;
        aggregates.forEach((aggregate) => {
          const canonical = `col[${JSON.stringify(column.name)}].${aggregate}`;
          const prefix = column.alias ? `col.${column.alias}` : `col[${JSON.stringify(column.name)}]`;
          suggestions.push({
            label: `${prefix}.${aggregate}`,
            insert: canonical,
            detail: `${column.type} aggregate`,
          });
        });
      });
    }

    [
      ['is_iso8601()', 'ISO-8601 helper'],
      ['parse_date()', 'Parse a text date'],
      ['is_finite()', 'Finite-number helper'],
      ['now()', 'Pinned run clock'],
    ].forEach(([value, detail]) => {
      suggestions.push({ label: value, insert: value, detail });
    });
    return suggestions;
  }

  private toggleTargetVisibility(show: boolean): void {
    if (this.targetField) {
      this.targetField.style.display = show ? '' : 'none';
    }
    if (this.targetCatalogField) {
      this.targetCatalogField.style.display = show ? '' : 'none';
    }
  }

  private focusTargetField(): void {
    if (this.focusApplied) {
      return;
    }
    const targetField = this.form.querySelector<HTMLInputElement>('#id_target_data_path');
    if (!targetField) {
      return;
    }
    window.requestAnimationFrame(() => {
      targetField.focus();
      if (typeof targetField.select === 'function') {
        targetField.select();
      }
    });
    this.focusApplied = true;
  }
}

const controllers = new WeakMap<HTMLFormElement, AssertionFormController>();

function initForm(form: HTMLFormElement): void {
  let controller = controllers.get(form);
  if (!controller) {
    controller = new AssertionFormController(form);
    controllers.set(form, controller);
    controller.init();
  } else {
    controller.refresh();
  }
}

export function initAssertionForms(root: ParentNode | Document = document): void {
  // Check if root itself is a matching form (happens when HTMX swaps
  // content and the <form> is the top-level element in the swap).
  if (root instanceof HTMLFormElement && root.hasAttribute('data-sv-assertion-form')) {
    initForm(root);
  }
  root.querySelectorAll<HTMLFormElement>('form[data-sv-assertion-form]').forEach(initForm);
}

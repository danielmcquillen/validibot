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

class AssertionFormController {
  private wrappers = new Map<ConditionalFieldName, HTMLElement>();
  private typeField: HTMLSelectElement | null;
  private operatorField: HTMLSelectElement | null;
  private targetField: HTMLElement | null;
  private targetCatalogField: HTMLElement | null;
  private targetPathInput: HTMLInputElement | null;
  private targetCatalogInput: HTMLInputElement | null;
  private celField: HTMLElement | null;
  private typeWrapper: HTMLElement | null;
  private shaclQueryField: HTMLTextAreaElement | null;
  private shaclFileInputInjected = false;
  private focusApplied = false;

  constructor(private form: HTMLFormElement) {
    this.typeField = this.form.querySelector<HTMLSelectElement>('#id_assertion_type');
    this.operatorField = this.form.querySelector<HTMLSelectElement>('#id_operator');
    this.targetPathInput = this.form.querySelector<HTMLInputElement>('[name="target_data_path"]');
    this.targetCatalogInput = this.form.querySelector<HTMLInputElement>('[name="target_catalog_entry"]');
    this.targetField = this.targetPathInput?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? null;
    this.targetCatalogField = this.targetCatalogInput?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? null;
    this.celField = this.form.querySelector<HTMLElement>('[name="cel_expression"]')?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? null;
    this.typeWrapper = this.typeField?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? null;
    this.shaclQueryField = this.form.querySelector<HTMLTextAreaElement>('#id_shacl_query');
  }

  init(): void {
    if (!this.typeField || !this.operatorField) {
      return;
    }
    this.bindEvents();
    this.injectShaclFileUpload();
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
    this.ensureCelPosition();
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

  /**
   * Force the CEL expression field to appear directly after the assertion type field.
   */
  private ensureCelPosition(): void {
    if (!this.celField || !this.typeWrapper) {
      return;
    }
    const parent = this.typeWrapper.parentElement;
    if (!parent) {
      return;
    }
    if (this.celField.previousElementSibling === this.typeWrapper) {
      return;
    }
    parent.insertBefore(this.celField, this.typeWrapper.nextElementSibling);
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

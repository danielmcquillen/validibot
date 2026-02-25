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
] as const;

type ConditionalFieldName = (typeof CONDITIONAL_FIELDS)[number];

const GENERAL_BASIC_FIELDS: ConditionalFieldName[] = ['coerce_types', 'treat_missing_as_null'];
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
  private celField: HTMLElement | null;
  private typeWrapper: HTMLElement | null;
  private focusApplied = false;

  constructor(private form: HTMLFormElement) {
    this.typeField = this.form.querySelector<HTMLSelectElement>('#id_assertion_type');
    this.operatorField = this.form.querySelector<HTMLSelectElement>('#id_operator');
    this.targetField = this.form.querySelector<HTMLElement>('[name="target_data_path"]')?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? null;
    this.targetCatalogField = this.form.querySelector<HTMLElement>('[name="target_catalog_entry"]')?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? null;
    this.celField = this.form.querySelector<HTMLElement>('[name="cel_expression"]')?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? null;
    this.typeWrapper = this.typeField?.closest<HTMLElement>(FIELD_WRAPPER_SELECTORS) ?? null;
  }

  init(): void {
    if (!this.typeField || !this.operatorField) {
      return;
    }
    this.bindEvents();
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
    if (typeValue === 'cel_expr') {
      this.showFields(['cel_expression', 'cel_allow_custom_signals']);
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

export function initAssertionForms(root: ParentNode | Document = document): void {
  const forms = root.querySelectorAll<HTMLFormElement>('form[data-sv-assertion-form]');
  forms.forEach((form) => {
    let controller = controllers.get(form);
    if (!controller) {
      controller = new AssertionFormController(form);
      controllers.set(form, controller);
      controller.init();
    } else {
      controller.refresh();
    }
  });
}

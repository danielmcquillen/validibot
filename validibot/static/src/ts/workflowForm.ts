/**
 * Keep the workflow input-contract authoring UI focused on the active mode.
 *
 * The workflow form always posts both textareas, but authors should only need
 * to look at the editor for the selected mode. When no mode is selected we
 * keep editors hidden until one has content or validation errors, so users do
 * not mistake an inactive editor for lost data.
 */

const WORKFLOW_FORM_SELECTOR = '[data-input-schema-section="true"]';
const MODE_HINT_SELECTOR = '[data-input-schema-mode-hint="true"]';
const MODE_FIELD_SELECTOR = 'input[name="input_schema_mode"]';
const MODE_EDITOR_SELECTOR = '[data-input-schema-mode-value]';

class WorkflowFormController {
  private readonly root: HTMLElement;
  private readonly modeFields: HTMLInputElement[];
  private readonly editors: HTMLElement[];
  private readonly modeHint: HTMLElement | null;

  constructor(root: HTMLElement) {
    this.root = root;
    this.modeFields = Array.from(
      root.querySelectorAll<HTMLInputElement>(MODE_FIELD_SELECTOR),
    );
    this.editors = Array.from(
      root.querySelectorAll<HTMLElement>(MODE_EDITOR_SELECTOR),
    );
    this.modeHint = root.querySelector<HTMLElement>(MODE_HINT_SELECTOR);
  }

  init(): void {
    this.syncEditors();
    this.modeFields.forEach((field) => {
      field.addEventListener('change', () => this.syncEditors());
    });
  }

  private syncEditors(): void {
    const activeMode = this.activeMode();

    if (activeMode) {
      this.toggleModeHint(false);
      this.editors.forEach((editor) => {
        this.setEditorVisibility(
          editor,
          editor.dataset.inputSchemaModeValue === activeMode,
        );
      });
      return;
    }

    let anyEditorVisible = false;
    this.editors.forEach((editor) => {
      const shouldShow = this.editorHasContentOrErrors(editor);
      this.setEditorVisibility(editor, shouldShow);
      anyEditorVisible = anyEditorVisible || shouldShow;
    });

    this.toggleModeHint(!anyEditorVisible);
  }

  private activeMode(): string {
    const checkedField = this.modeFields.find((field) => field.checked);
    return checkedField?.value ?? '';
  }

  private editorHasContentOrErrors(editor: HTMLElement): boolean {
    const textarea = editor.querySelector<HTMLTextAreaElement>('textarea');
    if (textarea && textarea.value.trim() !== '') {
      return true;
    }

    return Boolean(
      editor.querySelector(
        '.invalid-feedback, .is-invalid, .alert-danger, .errorlist',
      ),
    );
  }

  private setEditorVisibility(editor: HTMLElement, visible: boolean): void {
    editor.classList.toggle('d-none', !visible);
    editor.setAttribute('aria-hidden', visible ? 'false' : 'true');
  }

  private toggleModeHint(visible: boolean): void {
    if (!this.modeHint) {
      return;
    }
    this.modeHint.classList.toggle('d-none', !visible);
  }
}

export function initWorkflowForms(root: ParentNode | Document = document): void {
  root.querySelectorAll<HTMLElement>(WORKFLOW_FORM_SELECTOR).forEach((section) => {
    if (section.dataset.workflowFormInitialized === 'true') {
      return;
    }
    section.dataset.workflowFormInitialized = 'true';
    new WorkflowFormController(section).init();
  });
}

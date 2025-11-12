const WORKFLOW_LAUNCH_FORM_SELECTOR = '[data-workflow-launch-form]';

type ContentMode = 'upload' | 'paste';

function isContentMode(value: string | null): value is ContentMode {
  return value === 'upload' || value === 'paste';
}

/**
 * Lightweight controller for the workflow launch form.
 * Handles input-mode toggles, mirrors the dropzone state,
 * and keeps the submit button disabled until content is ready.
 */
class WorkflowLaunchFormController {
  private modeButtons: NodeListOf<HTMLButtonElement>;
  private uploadSection: HTMLElement | null;
  private pasteSection: HTMLElement | null;
  private payloadField: HTMLTextAreaElement | null;
  private fileInput: HTMLInputElement | null;
  private dropzone: HTMLElement | null;
  private dropzoneLabel: HTMLElement | null;
  private browseButton: HTMLButtonElement | null;
  private submitButton: HTMLButtonElement | null;
  private readonly emptyFileLabel: string;
  private readonly initialMode: ContentMode;

  constructor(private form: HTMLFormElement) {
    this.modeButtons = form.querySelectorAll<HTMLButtonElement>('[data-content-mode]');
    this.uploadSection = form.querySelector<HTMLElement>('[data-upload-section]');
    this.pasteSection = form.querySelector<HTMLElement>('[data-paste-section]');
    this.payloadField = this.pasteSection?.querySelector<HTMLTextAreaElement>('textarea') ?? null;
    this.fileInput = form.querySelector<HTMLInputElement>('[data-dropzone-input]');
    this.dropzone = form.querySelector<HTMLElement>('[data-dropzone]');
    this.dropzoneLabel = form.querySelector<HTMLElement>('[data-dropzone-file]');
    this.browseButton = form.querySelector<HTMLButtonElement>('[data-dropzone-browse]');
    this.submitButton = form.querySelector<HTMLButtonElement>('[data-launch-submit]');
    const labelValue = this.dropzoneLabel?.dataset.emptyLabel?.trim();
    this.emptyFileLabel = labelValue && labelValue.length > 0 ? labelValue : 'No file selected yet.';
    const preferredMode = this.form.dataset.defaultMode ?? '';
    this.initialMode = isContentMode(preferredMode) ? preferredMode : 'upload';
  }

  init(): void {
    this.bindModeButtons();
    this.bindDropzone();
    this.payloadField?.addEventListener('input', () => this.setSubmitState());
    this.setMode(this.initialMode);
    this.updateFileLabel();
    this.setSubmitState();
    this.bindSubmitDisabler();
  }

  private bindModeButtons(): void {
    this.modeButtons.forEach((button) => {
      button.addEventListener('click', () => {
        const mode = button.getAttribute('data-content-mode');
        if (!isContentMode(mode)) {
          return;
        }
        this.setMode(mode);
      });
    });
  }

  private bindDropzone(): void {
    if (!this.dropzone || !this.fileInput) {
      return;
    }

    const preventDefaults = (event: Event): void => {
      event.preventDefault();
      event.stopPropagation();
    };

    ['dragenter', 'dragover'].forEach((type) => {
      this.dropzone?.addEventListener(type, (event) => {
        preventDefaults(event);
        this.dropzone?.classList.add('is-dragover');
      });
    });

    ['dragleave', 'drop'].forEach((type) => {
      this.dropzone?.addEventListener(type, (event) => {
        preventDefaults(event);
        this.dropzone?.classList.remove('is-dragover');
      });
    });

    this.dropzone.addEventListener('drop', (event: DragEvent) => {
      preventDefaults(event);
      const files = event.dataTransfer?.files;
      if (!files || files.length === 0 || !this.fileInput) {
        return;
      }
      this.fileInput.files = files;
      this.updateFileLabel();
    });

    this.dropzone.addEventListener('click', () => this.fileInput?.click());
    this.dropzone.addEventListener('keypress', (event: KeyboardEvent) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        this.fileInput?.click();
      }
    });

    this.fileInput.addEventListener('change', () => this.updateFileLabel());
    if (this.browseButton) {
      this.browseButton.addEventListener('click', (event) => {
        event.preventDefault();
        this.fileInput?.click();
      });
    }
  }

  private setMode(mode: ContentMode): void {
    this.modeButtons.forEach((button) => {
      const buttonMode = button.getAttribute('data-content-mode');
      button.classList.toggle('active', buttonMode === mode);
    });

    if (this.uploadSection) {
      this.uploadSection.classList.toggle('d-none', mode !== 'upload');
    }
    if (this.pasteSection) {
      this.pasteSection.classList.toggle('d-none', mode !== 'paste');
    }

    if (mode === 'upload' && this.payloadField) {
      this.payloadField.value = '';
    }
    if (mode === 'paste' && this.fileInput) {
      this.fileInput.value = '';
    }
    if (mode === 'paste') {
      this.updateFileLabel();
    }

    this.setSubmitState();
  }

  private updateFileLabel(): void {
    if (!this.dropzoneLabel || !this.fileInput) {
      this.setSubmitState();
      return;
    }

    const hasFiles = Boolean(this.fileInput.files && this.fileInput.files.length > 0);
    if (hasFiles && this.fileInput.files) {
      this.dropzoneLabel.textContent = this.fileInput.files[0].name;
      this.dropzoneLabel.classList.add('text-body');
      this.dropzoneLabel.classList.remove('text-muted');
    } else {
      this.dropzoneLabel.textContent = this.emptyFileLabel;
      this.dropzoneLabel.classList.add('text-muted');
      this.dropzoneLabel.classList.remove('text-body');
    }
    this.setSubmitState();
  }

  private setSubmitState(): void {
    if (!this.submitButton) {
      return;
    }
    const hasPayload = Boolean(this.payloadField && this.payloadField.value.trim().length > 0);
    const hasAttachment = Boolean(this.fileInput && this.fileInput.files && this.fileInput.files.length > 0);
    const isReady = hasPayload || hasAttachment;
    this.submitButton.disabled = !isReady;
    this.submitButton.setAttribute('aria-disabled', String(!isReady));
  }

  private bindSubmitDisabler(): void {
    this.form.addEventListener('submit', () => {
      const interactiveElements = this.form.querySelectorAll<HTMLElement>('input, textarea, select, button, .workflow-dropzone');
      interactiveElements.forEach((element) => {
        if ('disabled' in element) {
          (element as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | HTMLButtonElement).disabled = true;
        }
        if (element.matches('.workflow-dropzone')) {
          element.setAttribute('aria-disabled', 'true');
          element.classList.add('is-disabled');
        }
      });
      if (this.submitButton) {
        this.submitButton.textContent = this.submitButton.dataset.submittingLabel ?? this.submitButton.textContent;
      }
    });
  }
}

export function initWorkflowLaunchForms(root: ParentNode | Document = document): void {
  const forms = root.querySelectorAll<HTMLFormElement>(WORKFLOW_LAUNCH_FORM_SELECTOR);
  forms.forEach((form) => {
    if (form.dataset.workflowLaunchInitialized === '1') {
      return;
    }
    try {
      const controller = new WorkflowLaunchFormController(form);
      controller.init();
      form.dataset.workflowLaunchInitialized = '1';
    } catch (error) {
      console.error('Failed to initialize workflow launch form', error);
    }
  });
}

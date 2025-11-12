const WORKFLOW_LAUNCH_FORM_SELECTOR = '[data-workflow-launch-form]';

type ContentMode = 'upload' | 'paste';

function isContentMode(value: string | null): value is ContentMode {
  return value === 'upload' || value === 'paste';
}

/**
 * Manages the workflow launch form, ensuring the upload/paste modes behave like tabs,
 * the dropzone mirrors the real file input, and the submit button stays disabled until
 * the user has provided either inline content or an attachment.
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
  private runStateHandler: ((event: Event) => void) | null = null;
  private submitHandler: ((event: Event) => void) | null = null;
  private errorHandler: ((event: Event) => void) | null = null;
  private nativeSubmitHandler: ((event: Event) => void) | null = null;

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
    this.syncDisabledState();
    this.bindRunStateWatcher();
    this.bindSubmissionWatcher();
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

  private syncDisabledState(): void {
    const disabled = this.form.dataset.runDisabled === 'true';
    this.toggleFormDisabled(disabled);
  }

  private toggleFormDisabled(disabled: boolean): void {
    this.form.dataset.runDisabled = String(disabled);
    const interactiveElements = this.form.querySelectorAll<HTMLElement>('input, textarea, select, button, .workflow-dropzone');
    interactiveElements.forEach((element) => {
      if ('disabled' in element) {
        (element as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | HTMLButtonElement).disabled = disabled;
      }
      element.classList.toggle('is-disabled', disabled);
      if (element.matches('.workflow-dropzone')) {
        element.setAttribute('aria-disabled', String(disabled));
      }
    });
  }

  private getStatusAreaElement(): HTMLElement | null {
    return this.form.ownerDocument?.getElementById('workflow-launch-status-area');
  }

  private syncRunStateFromStatusArea(): void {
    const statusArea = this.getStatusAreaElement();
    if (!statusArea) {
      return;
    }
    const runActive = statusArea.dataset.runActive === 'true';
    this.toggleFormDisabled(runActive);
  }

  private bindRunStateWatcher(): void {
    if (!window.htmx) {
      return;
    }

    this.runStateHandler = (event: Event) => {
      const customEvent = event as CustomEvent;
      const target = customEvent.detail?.target as HTMLElement | undefined;
      if (!target || target.id !== 'workflow-launch-status-area') {
        return;
      }
      this.syncRunStateFromStatusArea();
    };

    window.htmx.on('htmx:afterSwap', this.runStateHandler);
  }

  private bindSubmissionWatcher(): void {
    if (!window.htmx) {
      return;
    }

    this.submitHandler = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      const sourceElement = detail?.elt as HTMLElement | undefined;
      if (!sourceElement || (sourceElement !== this.form && !this.form.contains(sourceElement))) {
        return;
      }
      this.toggleFormDisabled(true);
    };

    this.errorHandler = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      const sourceElement = detail?.elt as HTMLElement | undefined;
      if (!sourceElement || (sourceElement !== this.form && !this.form.contains(sourceElement))) {
        return;
      }
      this.toggleFormDisabled(false);
    };

    this.nativeSubmitHandler = () => {
      this.toggleFormDisabled(true);
    };

    window.htmx.on('htmx:beforeRequest', this.submitHandler);
    window.htmx.on('htmx:responseError', this.errorHandler);
    this.form.addEventListener('submit', this.nativeSubmitHandler);
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

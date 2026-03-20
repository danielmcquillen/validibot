type Mode = 'upload' | 'paste' | 'form';

class WorkflowLaunchController {
  private uploadButton?: HTMLButtonElement | null;
  private pasteButton?: HTMLButtonElement | null;
  private formButton?: HTMLButtonElement | null;
  private uploadSection?: HTMLElement | null;
  private pasteSection?: HTMLElement | null;
  private formSection?: HTMLElement | null;
  private modeInput?: HTMLInputElement | null;
  private fileInput?: HTMLInputElement | null;
  private fileLabel?: HTMLElement | null;
  private dropzone?: HTMLElement | null;
  private browseButtons: NodeListOf<HTMLElement> | null = null;
  private validateInputUrl?: string | null;

  constructor(private readonly form: HTMLElement) {}

  init(defaultMode: Mode): void {
    this.uploadButton = this.form.querySelector<HTMLButtonElement>(
      '[data-content-mode="upload"]',
    );
    this.pasteButton = this.form.querySelector<HTMLButtonElement>(
      '[data-content-mode="paste"]',
    );
    this.formButton = this.form.querySelector<HTMLButtonElement>(
      '[data-content-mode="form"]',
    );
    this.uploadSection = this.form.querySelector<HTMLElement>('[data-upload-section]');
    this.pasteSection = this.form.querySelector<HTMLElement>('[data-paste-section]');
    this.formSection = this.form.querySelector<HTMLElement>('[data-form-section]');
    this.modeInput = this.form.querySelector<HTMLInputElement>('[data-input-mode-field]');
    this.fileInput = this.form.querySelector<HTMLInputElement>('[data-dropzone-input]');
    this.fileLabel = this.form.querySelector<HTMLElement>('[data-dropzone-file]');
    this.dropzone = this.form.querySelector<HTMLElement>('[data-dropzone]');
    this.browseButtons = this.form.querySelectorAll<HTMLElement>('[data-dropzone-browse]');
    this.validateInputUrl = this.form.getAttribute('data-validate-input-url');

    if (!this.uploadButton || !this.pasteButton) {
      return;
    }

    this.uploadButton.addEventListener('click', () => this.setMode('upload'));
    this.pasteButton.addEventListener('click', () => this.setMode('paste'));
    if (this.formButton) {
      this.formButton.addEventListener('click', () => this.setMode('form'));
    }

    this.bindDropzone();
    this.bindPreflightCheck();
    this.setMode(defaultMode);
    this.renderSelectedFile();
  }

  private setMode(mode: Mode): void {
    const isUpload = mode === 'upload';
    const isPaste = mode === 'paste';
    const isForm = mode === 'form';

    this.uploadButton?.classList.toggle('active', isUpload);
    this.pasteButton?.classList.toggle('active', isPaste);
    this.formButton?.classList.toggle('active', isForm);

    this.uploadButton?.setAttribute('aria-pressed', String(isUpload));
    this.pasteButton?.setAttribute('aria-pressed', String(isPaste));
    this.formButton?.setAttribute('aria-pressed', String(isForm));

    this.uploadSection?.classList.toggle('d-none', !isUpload);
    this.pasteSection?.classList.toggle('d-none', !isPaste);
    this.formSection?.classList.toggle('d-none', !isForm);

    if (this.modeInput) {
      this.modeInput.value = mode;
    }
  }

  private bindDropzone(): void {
    if (!this.fileInput || !this.fileLabel) {
      return;
    }

    const resetDragState = () => this.dropzone?.classList.remove('is-dragover');
    const setFileFromList = (files?: FileList | null) => {
      if (!files || !files.length) {
        return;
      }
      this.fileInput!.files = files;
      this.fileInput!.dispatchEvent(new Event('change', { bubbles: true }));
    };

    this.fileInput.addEventListener('change', () => {
      this.setMode('upload');
      this.renderSelectedFile();
    });

    this.browseButtons?.forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
      });
    });

    this.dropzone?.addEventListener('click', () => this.fileInput!.click());
    this.dropzone?.addEventListener('dragover', (event) => {
      event.preventDefault();
      this.dropzone?.classList.add('is-dragover');
    });
    this.dropzone?.addEventListener('dragleave', resetDragState);
    this.dropzone?.addEventListener('drop', (event) => {
      event.preventDefault();
      const files = event.dataTransfer?.files;
      setFileFromList(files);
      resetDragState();
    });
  }

  private renderSelectedFile(): void {
    if (!this.fileInput || !this.fileLabel) {
      return;
    }
    const emptyLabel = this.fileLabel.dataset.emptyLabel || '';
    const selected = this.fileInput.files && this.fileInput.files[0];
    this.fileLabel.textContent = selected ? selected.name : emptyLabel;
  }

  private bindPreflightCheck(): void {
    if (!this.validateInputUrl) {
      return;
    }

    // Form-mode check button
    const checkBtn = this.form.querySelector<HTMLButtonElement>('[data-check-input]');
    const statusPanel = this.form.querySelector<HTMLElement>('[data-validation-status]');
    if (checkBtn && statusPanel) {
      checkBtn.addEventListener('click', () => {
        this.runPreflightCheck('form', statusPanel);
      });
    }

    // Paste-mode check button
    const checkBtnPaste = this.form.querySelector<HTMLButtonElement>(
      '[data-check-input-paste]',
    );
    const statusPanelPaste = this.form.querySelector<HTMLElement>(
      '[data-validation-status-paste]',
    );
    if (checkBtnPaste && statusPanelPaste) {
      checkBtnPaste.addEventListener('click', () => {
        this.runPreflightCheck('paste', statusPanelPaste);
      });
    }
  }

  /**
   * Run a preflight schema-validation check via fetch to the
   * validate-input endpoint.  The response is a server-rendered
   * HTML partial that replaces the status panel content.
   *
   * This is safe to inject because the HTML comes from our own
   * Django view (schema_validation_status.html) which only renders
   * server-generated, escaped content — never raw user input.
   */
  private async runPreflightCheck(
    inputMode: string,
    statusPanel: HTMLElement,
  ): Promise<void> {
    if (!this.validateInputUrl) {
      return;
    }

    const formEl = this.form.closest('form') as HTMLFormElement;
    const formData = new FormData(formEl);
    formData.set('input_mode', inputMode);

    try {
      const response = await fetch(this.validateInputUrl, {
        method: 'POST',
        body: formData,
      });
      if (response.ok) {
        const html = await response.text();
        // Replace status panel with server-rendered partial.
        statusPanel.innerHTML = html;  // nosec: trusted server response
      }
    } catch {
      // Silently fail — preflight is optional UX
    }
  }
}

export function initWorkflowLaunch(root: ParentNode | Document = document): void {
  const forms = Array.from(
    root.querySelectorAll<HTMLElement>('[data-workflow-launch-form="true"]'),
  );
  forms.forEach((form) => {
    const controller = new WorkflowLaunchController(form);
    const rawMode = form.getAttribute('data-default-mode') as Mode | null;
    let defaultMode: Mode = 'upload';
    if (rawMode === 'paste' || rawMode === 'form') {
      defaultMode = rawMode;
    }
    controller.init(defaultMode);
  });
}

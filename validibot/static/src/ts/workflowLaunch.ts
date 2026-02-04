type Mode = 'upload' | 'paste';

class WorkflowLaunchController {
  private uploadButton?: HTMLButtonElement | null;
  private pasteButton?: HTMLButtonElement | null;
  private uploadSection?: HTMLElement | null;
  private pasteSection?: HTMLElement | null;
  private modeInput?: HTMLInputElement | null;
  private fileInput?: HTMLInputElement | null;
  private fileLabel?: HTMLElement | null;
  private dropzone?: HTMLElement | null;
  private browseButtons: NodeListOf<HTMLElement> | null = null;

  constructor(private readonly form: HTMLElement) {}

  init(defaultMode: Mode): void {
    this.uploadButton = this.form.querySelector<HTMLButtonElement>(
      '[data-content-mode="upload"]',
    );
    this.pasteButton = this.form.querySelector<HTMLButtonElement>(
      '[data-content-mode="paste"]',
    );
    this.uploadSection = this.form.querySelector<HTMLElement>('[data-upload-section]');
    this.pasteSection = this.form.querySelector<HTMLElement>('[data-paste-section]');
    this.modeInput = this.form.querySelector<HTMLInputElement>('[data-input-mode-field]');
    this.fileInput = this.form.querySelector<HTMLInputElement>('[data-dropzone-input]');
    this.fileLabel = this.form.querySelector<HTMLElement>('[data-dropzone-file]');
    this.dropzone = this.form.querySelector<HTMLElement>('[data-dropzone]');
    this.browseButtons = this.form.querySelectorAll<HTMLElement>('[data-dropzone-browse]');

    if (!this.uploadButton || !this.pasteButton || !this.uploadSection || !this.pasteSection) {
      return;
    }

    this.uploadButton.addEventListener('click', () => this.setMode('upload'));
    this.pasteButton.addEventListener('click', () => this.setMode('paste'));

    this.bindDropzone();
    this.setMode(defaultMode);
    this.renderSelectedFile();
  }

  private setMode(mode: Mode): void {
    const isPaste = mode === 'paste';

    this.uploadButton?.classList.toggle('active', !isPaste);
    this.pasteButton?.classList.toggle('active', isPaste);
    this.uploadButton?.setAttribute('aria-pressed', String(!isPaste));
    this.pasteButton?.setAttribute('aria-pressed', String(isPaste));

    this.uploadSection?.classList.toggle('d-none', isPaste);
    this.pasteSection?.classList.toggle('d-none', !isPaste);

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
      // Keep the browser-native FileList while ensuring our UI reflects the change.
      this.fileInput!.files = files;
      this.fileInput!.dispatchEvent(new Event('change', { bubbles: true }));
    };

    this.fileInput.addEventListener('change', () => {
      this.setMode('upload');
      this.renderSelectedFile();
    });

    // The browse button is a <label for="..."> which natively opens the file picker.
    // We only need to stop propagation to prevent the dropzone click handler from
    // also triggering (which would open the file picker twice).
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
}

export function initWorkflowLaunch(root: ParentNode | Document = document): void {
  const forms = Array.from(
    root.querySelectorAll<HTMLElement>('[data-workflow-launch-form="true"]'),
  );
  forms.forEach((form) => {
    const controller = new WorkflowLaunchController(form);
    const defaultMode =
      (form.getAttribute('data-default-mode') as Mode | null) === 'paste'
        ? 'paste'
        : 'upload';
    controller.init(defaultMode);
  });
}

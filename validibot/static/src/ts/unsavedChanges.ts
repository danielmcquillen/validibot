// Unsaved-changes guard.
//
// Protects any form marked `data-unsaved-guard` from silently losing edits:
//   • a native `beforeunload` prompt catches tab-close and hard navigation
//   • a custom "Discard changes?" modal (#unsavedChangesModal) intercepts
//     clicks on a `data-unsaved-cancel` link while the form is dirty
//   • Save (a normal submit) is treated as an intentional, clean exit
//
// In-page HTMx actions inside the form (e.g. applying an inferred schema or
// adding a column) are genuine edits but fire no input/change event, so a swap
// whose target lives inside the form also marks it dirty.

const GUARD_SELECTOR = '[data-unsaved-guard]';

function bindGuard(form: HTMLFormElement): void {
  if (form.dataset.unsavedGuardBound === 'true') {
    return;
  }
  form.dataset.unsavedGuardBound = 'true';

  let dirty = false;
  let leaving = false;

  const markDirty = (): void => {
    dirty = true;
  };

  form.addEventListener('input', markDirty);
  form.addEventListener('change', markDirty);

  // HTMx swaps inside the form (infer / import / apply / add column) change the
  // unsaved schema but emit no input/change event, so flag them explicitly.
  document.body.addEventListener('htmx:afterSwap', (event: Event) => {
    const target = (event as CustomEvent).detail?.target as Node | undefined;
    if (target && form.contains(target)) {
      markDirty();
    }
  });

  // A normal submit (Save) is an intentional, clean exit — let it through.
  form.addEventListener('submit', () => {
    leaving = true;
    dirty = false;
  });

  window.addEventListener('beforeunload', (event: BeforeUnloadEvent) => {
    if (dirty && !leaving) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  const modalEl = document.getElementById('unsavedChangesModal');
  let pendingHref: string | null = null;

  form
    .querySelectorAll<HTMLAnchorElement>('[data-unsaved-cancel]')
    .forEach((link) => {
      link.addEventListener('click', (event) => {
        if (!dirty || !modalEl) {
          return; // clean, or no modal available — let the link navigate.
        }
        event.preventDefault();
        pendingHref = link.getAttribute('href');
        window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
      });
    });

  if (modalEl) {
    modalEl
      .querySelector<HTMLButtonElement>('[data-unsaved-confirm]')
      ?.addEventListener('click', () => {
        leaving = true;
        dirty = false;
        if (pendingHref) {
          window.location.href = pendingHref;
        }
      });
  }
}

export function initUnsavedChanges(
  root: ParentNode | Document = document,
): void {
  const forms = new Set<HTMLFormElement>();
  if (root instanceof Element && root.matches(GUARD_SELECTOR)) {
    forms.add(root as HTMLFormElement);
  }
  root
    .querySelectorAll<HTMLFormElement>(GUARD_SELECTOR)
    .forEach((form) => forms.add(form));
  forms.forEach(bindGuard);
}

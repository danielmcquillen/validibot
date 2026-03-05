/**
 * Template Variable Editor — conditional field visibility.
 *
 * Each template variable card has a "Type" radio group (number / text / choice).
 * This module shows or hides the type-dependent field sections:
 *
 *   - `.tplvar-number-fields` — visible when Type = "number"
 *   - `.tplvar-choice-fields` — visible when Type = "choice"
 *
 * Uses Bootstrap's `d-none` utility class for show/hide toggling.
 * Runs on DOMContentLoaded and on htmx:onLoad to handle dynamic content.
 */

function updateFieldVisibility(card: HTMLElement): void {
  const selected = card.querySelector<HTMLInputElement>(
    'input[name$="_variable_type"]:checked',
  );
  const selectedType = selected?.value || 'text';

  const numberFields =
    card.querySelectorAll<HTMLElement>('.tplvar-number-fields');
  const choiceFields =
    card.querySelectorAll<HTMLElement>('.tplvar-choice-fields');

  numberFields.forEach((el) =>
    el.classList.toggle('d-none', selectedType !== 'number'),
  );
  choiceFields.forEach((el) =>
    el.classList.toggle('d-none', selectedType !== 'choice'),
  );
}

export function initTemplateVariableEditor(
  root: ParentNode | Document = document,
): void {
  const cards = root.querySelectorAll<HTMLElement>('[data-tplvar-card]');

  cards.forEach((card) => {
    // Listen for type radio changes
    const typeInputs = card.querySelectorAll<HTMLInputElement>(
      'input[name$="_variable_type"]',
    );
    typeInputs.forEach((input) => {
      input.addEventListener('change', () => updateFieldVisibility(card));
    });

    // Set initial visibility based on current selection
    updateFieldVisibility(card);
  });
}

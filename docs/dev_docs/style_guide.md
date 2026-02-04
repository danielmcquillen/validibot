# Validibot Style Guide

This guide captures the small-but-important UI conventions we rely on across the app. Keep it friendly, consistent, and discoverable.

## Buttons and Icons

- Use icon-only buttons for destructive actions:
  - Prefer the trash icon (`bi-trash`) for remove/delete.
  - Always include a tooltip with the verb and target (e.g., "Delete project Alpha", "Remove member jane"). Add `aria-label` with the same text for accessibility.
- Keep primary actions as labeled buttons; reserve icon-only buttons for secondary/destructive actions.

## Cards and Help Blocks

- Use `app-card` for standard content cards to inherit padding and shadows.
- Use `help-card` when you present in-context guidance. These cards get a subtle highlight to signal “explanation” rather than “action.”
- When explaining multi-step or role-related behavior, aim for short paragraphs and, if helpful, a compact table that calls out what’s included and who it’s for.

## Tooltips

- Use Bootstrap tooltips (`data-bs-toggle="tooltip"`) for icon-only buttons or abbreviations.
- Tooltips should be concise and action-oriented (e.g., "Delete project Alpha" rather than "Click to delete").

## Roles and Permissions UI

- Show implied roles as checked + disabled to reflect cumulative permissions.
- If a higher role is selected (Admin, Author), lower roles should display as disabled to avoid confusion about what’s included.
- Keep the roles help text close to the controls; summarize which roles auto-include others and how to compose specific combinations.

## Empty States

- Provide a short, reassuring empty state: what’s missing and the next step (e.g., “No projects yet. Create a project to organize workflows.”).

## Language and Tone

- Write in a clear, conversational tone. Prefer short sentences and avoid jargon.
- Use action-first copy on buttons and headers (“Create project”, “Invite member”).

## Accessibility

- Every icon-only control needs an `aria-label`.
- Maintain sufficient contrast on badges, alerts, and text-muted elements; prefer default Bootstrap palette to stay within contrast bounds.

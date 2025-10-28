# Related Libraries

SimpleValidations collaborates with two sibling projects that live alongside this repository. This page records their roles so future changes keep the three codebases aligned.

## Project Layout

- `../sv_shared`: Shared Python package installed into this project as the `sv-shared` dependency. Source lives in the neighbouring repository, but a vendored version is located inside the virtual environment at `.venv/lib/python3.x/site-packages/sv_shared`. Any changes should be made in sv_shared, and then bump the dependency here.
- `../sv_modal`: Django + Modal.com orchestration code that runs remote validation jobs. There is no direct Python path import from this project, so use filesystem-relative imports or API contracts when wiring up engines.

## sv_shared

`sv_shared` defines payload schemas, enums, and helpers used by both the Django app and Modal workers. When implementing features in `simplevalidations`:

- Inspect the authoritative models (for example, `../sv_shared/energyplus/models.py`) before changing serializers or engine payloads.
- If a contract change is required, update `sv_shared` first, publish a compatible version, and upgrade `requirements/base.txt` (plus regenerate the legacy requirement sets).
- Document any breaking or additive change in both repos—ideally via README updates or release notes—so engineers know which version pairs are compatible.

## sv_modal

`sv_modal` hosts the Modal.com workflow runners. Engines in `simplevalidations` (such as `EnergyPlusEngine`) communicate with this project via API calls, queues, or job triggers defined there.

- Before editing engine code, review the corresponding handler in `../sv_modal` to confirm expected input/output contracts.
- When adding new engine features, implement and test the Modal-side worker in `sv_modal` first, then update `simplevalidations` to call the new behavior.
- Capture any cross-repo assumptions in docstrings or comments so future developers know where to look if behavior changes.

## Working Across Repos

- Keep all three repositories checked out in the same parent folder so relative references remain valid.
- During development, open the relevant modules in `../sv_shared` and `../sv_modal` alongside the Django code to avoid contract drift.
- When touching integrations, note follow-up actions (tests, dependency bumps, deployment sequencing) in the tracking issue or project board.

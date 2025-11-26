# Codex Agent Guide

## Mission

- Keep SimpleValidations maintainable and transparent for a single developer.
- Prefer straightforward Django patterns; document any advanced technique you touch.
- Follow google guidelines for Python development.

## Code documentation

- Follow Google guidelines for Python documentation. All classes should have a block of comments
  describing what the class does and how it relates to its wider context.

## Quick Links

- Developer knowledge base: `docs/dev_docs/` (start with `docs/dev_docs/index.md`).
- Workflow architecture details: `docs/dev_docs/overview/how_it_works.md`.
- Working agreements: `docs/dev_docs/overview/platform_overview.md#working-agreements-for-developers`.

## Cross-Repo Awareness

- Always consider the neighbouring projects `../sv_modal` and `sv_shared` (installed here, source in `../sv_shared`) when assessing behaviour or authoring code, especially for integrations like EnergyPlus.
- At the start of a task, open the relevant modules in those repos so their current contracts guide decisions made in `simplevalidations`.

## Collaboration Principles

- Speak plainly, surface risks first, and reference file paths with line numbers when reviewing.
- Update docs or inline comments whenever you introduce behaviour that is not obvious at first glance.
- When you see ambiguity, clarify assumptions with the team before coding.
- Default to Django Crispy Forms for HTML forms. If a legacy form is not
  using Crispy, capture a TODO in the docs or tracking issue before diverging.
- Do **not** add/commit/push or otherwise modify git state unless explicitly instructed. Avoid staging files or running git commands that change the repo by default. It's ok to read git history or look at git status to figure out what has changed.

## Ready-to-Help Checklist

- [ ] Read the relevant section in the developer docs before changing a module.
- [ ] Confirm workflow, submission, and run objects stay aligned on org/project/user relationships.
- [ ] Note any follow-up work or open questions directly in the docs or tracking issue.

## Default Response Pattern

1. Summarize the change or finding in a sentence.
2. List blockers, bugs, or concerns ordered by impact.
3. Offer the next two or three actions the team can take (tests, docs, clean-up).

_All project context lives in the developer documentation. Keep this guide short, and keep the docs rich._

## Documentation tone

When updating docs, write in a clear, friendly, conversational style. Use short paragraphs instead of dense bullet lists, avoid jargon when a plainer phrase works, and favor clarity over terseness.

## Running commands, tests, etc.

Use uv to run commands, tests etc. so the relevant virtual environment is automatically made available.
Project dependencies live in `pyproject.toml`; add the `dev` extra when you need tooling (for example,
`uv run --extra dev pytest`). Production-only dependencies live under the `prod` extra.
Whenever you run a command that involves Django, be sure to first load the environment variables
contained in `_envs/local/django.env` by running the `set-env.sh` script.

When you update dependencies remember to regenerate the legacy requirement sets for Heroku:

```
uv export --no-dev --output-file requirements/base.txt
uv export --no-dev --extra prod --output-file requirements/production.txt
uv export --extra dev --output-file requirements/local.txt
```

## Be consistent when defining constants

We try to be consistent about putting constants in the relevant app's constants.py module as either a TextChoices or an Enum class, and then using those constants in
code rather than doing comparisons to individual strings.

Don't do something like this at the top of a module that holds forms:

```
from django.utils.translation import gettext_lazy as _

AI_TEMPLATES = (
    ("ai_critic", _("AI Critic")),
    ("policy_check", _("Policy Check")),
)

```

Instead, define a TextChoices class, or just an Enum if the constants aren't for a model or form. Use the relevant app's constants.py module:

```
from django.db import models
from django.utils.translation import gettext_lazy as _

class AITemplates(models.TextChoices):
AI_CRITIC = "AI_CRITIC", _("AI Critic")
AI*POLICY_CHECK = "AI_POLICY_CHECK", _("AI Policy Check")

```

# API

The API we are creating should follow best practices for REST API implementations.

The typical structure for a REST API error reponse is explained in the following table:

| Field | Type | Purpose | Example |
| ----- | ---- | ------- | ------- |

| detail | string | Human-readable message about what went wrong.| "This workflow is not active and cannot accept new runs."|
| code | string | Machine-readable short code for programmatic handling. | "workflow_inactive" |
| (optional) status | integer | HTTP status repeated in the body (some APIs include this). | 409 |
| (optional) type | string (URI)| Error type identifier (useful in JSON:API or RFC 7807). | "https://api.example.com/errors/workflow_inactive" |
| (optional) errors| array | List of field-level issues (for validation errors).| [{ "field": "name", "message": "This field is required." }]|

# Tests

Always create tests that can ensure added features work correctly. Don't create _too many_ tests, just
a few key ones that make sure things work correctly.

Note that integration tests should go in the top-level tests/ folder. Integration tests want the system to behave
as closely as possible to the runtime system. These tests often span multiple pages or steps.

Please try to always use proper Django TestCase classes with good cleanup and tear down structure.

# Documentation

Make sure to always add code documentation to the top of all classes. Explain clearly what the class is for
and include a relevant example if helpful.

# Coding Standards

Make sure to always add trailing commas so we don't get Rust linting errors (Trailing comma missing, COM812)

All other code should be correct and not cause the Rust linter to fail. For example, don't make really long string lines, instead break them up by using parenthesis and newslines.

When possible, try not to use 'magic numbers' in code (avoid the linting error PLR2004). For example use HTTPStatus rather than integers.
Otherwise, create a static variable at the top of the module, and then use that variable in the code. For example MAX_NUMBER_TRIES=3 (and then use MAX_NUMBER_TRIES in code).

# HTMx + Bootstrap Modal Forms Pattern

Bootstrap modals with HTMx form submissions require careful state management to avoid common issues like disappearing modals, stuck backdrops, or persisted form errors. Follow this pattern for all modal forms in SimpleValidations.

## The Problem

When HTMx swaps content using `hx-swap="outerHTML"` on a Bootstrap modal, it destroys the modal's DOM structure including the Bootstrap modal JavaScript instance. This causes:

- Modal disappears but backdrop remains stuck
- Validation errors flash briefly then vanish
- Form state persists between modal opens
- Bootstrap modal events stop firing

## The Solution: Two-Template Pattern

Use separate templates for the modal wrapper and the modal content, with `innerHTML` swap strategy.

### Template Structure

**1. Full Modal Template** (`modal_[name].html`)

```django
{% load i18n crispy_forms_tags core_tags %}

{% with modal_id=modal_id|default:"modal-default" modal_title=modal_title|default:_("Modal Title") %}
  <div class="modal fade"
       id="{{ modal_id }}"
       tabindex="-1"
       aria-labelledby="{{ modal_id }}-label"
       aria-hidden="true"
       hx-get="{% org_url 'app:view_name' pk=object.id %}{% if param %}?param={{ param }}{% endif %}"
       hx-target="#{{ modal_id }}-content"
       hx-swap="innerHTML"
       hx-trigger="show.bs.modal">
    <div class="modal-dialog modal-lg modal-dialog-scrollable">
      <div class="modal-content" id="{{ modal_id }}-content">
        {# Initial content - will be replaced by GET request when modal opens #}
        <div class="modal-header">
          <h5 class="modal-title" id="{{ modal_id }}-label">{{ modal_title }}</h5>
          <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="{% trans 'Close' %}"></button>
        </div>
        <form hx-post="{% org_url 'app:view_name' pk=object.id %}"
              hx-target="#{{ modal_id }}-content"
              hx-swap="innerHTML"
              novalidate>
          <div class="modal-body">
            {% csrf_token %}
            {{ modal_form|crispy }}
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">{% trans "Cancel" %}</button>
            <button type="submit" class="btn btn-primary">{% trans "Save" %}</button>
          </div>
        </form>
      </div>
    </div>
  </div>
{% endwith %}
```

**Key attributes on the modal div:**

- `hx-get`: URL to fetch fresh form content
- `hx-target="#{{ modal_id }}-content"`: Target the `modal-content` div, NOT the modal itself
- `hx-swap="innerHTML"`: Replace content INSIDE the target, preserving the modal structure
- `hx-trigger="show.bs.modal"`: Fetch fresh content when Bootstrap's modal show event fires

**2. Form-Only Template** (`modal_[name]_form.html`)

```django
{% load i18n crispy_forms_tags core_tags %}

<div class="modal-header">
  <h5 class="modal-title" id="{{ modal_id }}-label">{{ modal_title }}</h5>
  <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="{% trans 'Close' %}"></button>
</div>
<form hx-post="{% org_url 'app:view_name' pk=object.id %}"
      hx-target="#{{ modal_id }}-content"
      hx-swap="innerHTML"
      novalidate>
  <div class="modal-body">
    {% csrf_token %}
    {% if modal_form.non_field_errors %}
      <div class="alert alert-danger">
        {{ modal_form.non_field_errors }}
      </div>
    {% endif %}
    {{ modal_form|crispy }}
  </div>
  <div class="modal-footer">
    <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">{% trans "Cancel" %}</button>
    <button type="submit" class="btn btn-primary">{% trans "Save" %}</button>
  </div>
</form>
```

**Important notes:**

- Only show alert if `modal_form.non_field_errors` exists (not `modal_form.errors`), otherwise empty alert bar appears
- Form must include same HTMx attributes: `hx-post`, `hx-target`, `hx-swap`
- This template is returned on validation errors and GET requests

### View Implementation

```python
from django.shortcuts import render, get_object_or_404
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView

class MyModalFormView(FormView):
    form_class = MyForm

    def get(self, request, *args, **kwargs):
        """Handle GET requests to return fresh form content for HTMx modal."""
        # Get any parameters needed to configure the form
        param = request.GET.get("param")

        # Create fresh form instance
        form = self.form_class(initial={"param": param})

        # Handle HTMx requests
        if request.headers.get("HX-Request"):
            return render(
                request,
                "app/partials/modal_form_name_form.html",  # Form-only template
                {
                    "object": self.get_object(),
                    "modal_form": form,
                    "modal_id": "modal-form-name",
                    "modal_title": _("Modal Title"),
                },
            )

        # Non-HTMx GET request - redirect to appropriate page
        return redirect("app:detail", pk=self.kwargs["pk"])

    def post(self, request, *args, **kwargs):
        """Handle form submission."""
        form = self.form_class(request.POST)

        if form.is_valid():
            # Save the form
            instance = form.save(commit=False)
            instance.related_object = self.get_object()
            instance.save()

            messages.success(request, _("Successfully saved."))

            # Handle HTMx requests - use HX-Redirect header to close modal and refresh page
            if request.headers.get("HX-Request"):
                response = HttpResponse(status=204)  # 204 No Content
                response["HX-Redirect"] = reverse("app:detail", kwargs={"pk": self.kwargs["pk"]})
                return response

            # Non-HTMx fallback
            return redirect("app:detail", pk=self.kwargs["pk"])

        # Form has errors
        if request.headers.get("HX-Request"):
            # Return form-only template with errors
            # IMPORTANT: Use status=200, not 400! HTMx only processes 2xx responses by default
            return render(
                request,
                "app/partials/modal_form_name_form.html",  # Form-only template
                {
                    "object": self.get_object(),
                    "modal_form": form,  # Form with errors
                    "modal_id": "modal-form-name",
                    "modal_title": _("Modal Title"),
                },
                status=200,  # Must be 200 for HTMx to swap content
            )

        # Non-HTMx fallback
        messages.error(request, _("Please correct the errors below."))
        return redirect("app:detail", pk=self.kwargs["pk"])
```

## Critical HTMx + Django Form Rules

### 1. **Status Codes Matter**

- **Success (valid form)**: Return `204 No Content` with `HX-Redirect` header
- **Validation errors**: Return `200 OK` with error form template
- **Never use 400/422 for validation errors** - HTMx only processes 2xx responses by default
- To use 4xx status codes, you'd need to configure HTMx's `hx-swap` with error handling

### 2. **Swap Strategy: innerHTML, Not outerHTML**

- **Always use `hx-swap="innerHTML"`** when targeting modal content
- Target the `modal-content` div with an ID: `hx-target="#modal-id-content"`
- **Never use `hx-swap="outerHTML"`** on Bootstrap modals - it destroys the modal instance

### 3. **Form Reset on Modal Open**

- Add `hx-get` to the modal div with `hx-trigger="show.bs.modal"`
- This fetches fresh form content every time the modal opens
- Prevents validation errors from persisting between uses
- View's GET handler must return the form-only template

### 4. **Error Display**

- Only show alert div if `{% if modal_form.non_field_errors %}`
- Don't use `{% if modal_form.errors %}` - it's truthy even when only field errors exist
- Field errors are handled by Crispy Forms within the form rendering

### 5. **Closing Modal After Success**

- Use `HX-Redirect` header to close modal and refresh/redirect page
- Return `204 No Content` status with the header
- Example: `response["HX-Redirect"] = reverse("app:detail", kwargs={...})`
- HTMx will perform a full page navigation, closing the modal

### 6. **URL Parameters**

- Pass parameters via query string in `hx-get` URL
- Example: `{% if run_stage %}?run_stage={{ run_stage }}{% endif %}`
- View's GET handler reads from `request.GET.get("param")`
- View's POST handler reads from `request.POST.get("param")`

### 7. **Template Context Variables**

- Both templates need same context: `modal_form`, `modal_id`, `modal_title`, plus any domain objects
- Use consistent variable names across both templates
- Pass `modal_id` to enable multiple instances of same modal type

## Common Mistakes to Avoid

1. ❌ **Using `hx-swap="outerHTML"` on modals** → Destroys Bootstrap modal instance
2. ❌ **Returning 400 status on validation errors** → HTMx won't swap content
3. ❌ **Not implementing GET handler** → Form won't reset between opens
4. ❌ **Using `{% if modal_form.errors %}` for alert** → Shows empty alert on field errors
5. ❌ **Targeting the modal div instead of modal-content** → Replaces entire modal structure
6. ❌ **Not using `hx-trigger="show.bs.modal"`** → Form persists old state
7. ❌ **Adding `hx-on` event handlers to re-show modal** → Conflicts with Bootstrap modal lifecycle

## Example: Validator Signal Create Modal

See `validations/templates/validations/library/partials/modal_signal_create.html` and
`modal_signal_create_form.html` with `validations/views.py::ValidatorSignalCreateView` for a complete working example.

## Debugging Tips

- **Modal disappears but backdrop stays**: Using outerHTML swap or targeting wrong element
- **Empty alert bar**: Using `modal_form.errors` instead of `modal_form.non_field_errors`
- **Form shows old errors**: GET handler not implemented or not triggered on modal show
- **Content not swapping on error**: Check status code (must be 200)
- **Modal not closing on success**: Need `HX-Redirect` header with 204 status

Use Firefox/Chrome dev tools Network tab to inspect:

- HTMx request headers: `HX-Request: true`
- Response status codes
- Response headers: `HX-Redirect`
- Response content (should be form-only template on errors)

# Coding Standards

Make sure to always add trailing commas so we don't get Rust linting errors (Trailing comma missing, COM812)

All other code should be correct and not cause the Rust linter to fail. For example, don't make really long string lines, instead break them up by using parenthesis and newslines.

When possible, try not to use 'magic numbers' in code (avoid the linting error PLR2004). For example use HTTPStatus rather than integers.
Otherwise, create a static variable at the top of the module, and then use that variable in the code. For example MAX_NUMBER_TRIES=3 (and then use MAX_NUMBER_TRIES in code).

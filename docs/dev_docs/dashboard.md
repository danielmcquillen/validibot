# Dashboard Module

The dashboard surface collects operational metrics for the currently scoped
organization. The goal is to keep the implementation incremental-friendly so we
can ship additional widgets without revisiting the foundations each time.

## Architecture Overview

- **Views** – `simplevalidations.dashboard.views.MyDashboardView` builds the
  landing page, resolves the selected `time_range`, and queues the registered
  widgets for HTMX loading. `WidgetDetailView` is the single HTMX endpoint that
  renders widget bodies on demand using `TemplateResponse` so tests can easily
  inspect context.
- **Widget registry** – `simplevalidations.dashboard.widgets.base` defines the
  `DashboardWidget` base-class, the registry, and validation during
  registration. Each widget subclass provides a slug, title, template, and
  `get_context_data` implementation. Built-ins live in
  `metrics.py` (scalar cards) and `timeseries.py` (line charts).
- **Time ranges** – `dashboard.time_ranges` exposes curated presets (1h → 90d)
  and resolves them to concrete `ResolvedTimeRange` instances. Bucket
  selection (hour vs. day) happens here so individual widgets do not need to
  repeat the logic.
- **Data services** – `dashboard.services.generate_time_series` and
  `build_chart_payload` shape ORM results into Chart.js config dictionaries and
  ensure gaps are zero-filled so the charts stay readable.

The Django app auto-imports `simplevalidations.dashboard.widgets` in
`DashboardConfig.ready()` to populate the registry during startup.

## Data Sources & Scoping

Widgets resolve the organization with `request.user.get_current_org()` and
apply it to each query:

- `TotalValidationsWidget` counts `ValidationRun` rows per org/time window.
- `TotalErrorsWidget` counts `ValidationFinding` rows with severity `ERROR`.
- `EventsTimeSeriesWidget` aggregates `TrackingEvent` volume.
- `UsersTimeSeriesWidget` counts distinct `TrackingEvent.user_id` values by
  interval.
- Login/logout signals record activity as `user.logged_in` / `user.logged_out`
  events so the user chart has data even before validation runs exist.

Every query filters on `created__gte/start` and `created__lt/end` using the
resolved time range. When you add new widgets reuse the helper functions to
avoid cross-tenant leaks.

## HTMX Flow & Loading Experience

1. `my_dashboard.html` renders lightweight placeholders for each registered
   widget. Every placeholder carries `hx-get` attributes pointing to the widget
   detail endpoint and triggers both on `load` and the custom
   `dashboard:refresh` event.
2. The inline script rewrites the `hx-get` URL when a new time range is
   selected and dispatches `dashboard:refresh` so every widget reloads without a
   full-page refresh. The history state updates to keep URLs shareable.
3. `WidgetDetailView` sends back the fully styled widget markup. Because the
   outer wrapper retains the HTMX attributes (minus the initial `load` trigger)
   subsequent refreshes work transparently.
4. If a widget has no data, the template shows an empty-state panel instead of
   an empty chart.

## Front-end Integration

- Chart.js is now shipped via `package.json`; bundling happens in
  `static/src/ts/project.ts`. We register the default chart set, expose
  `window.Chart`, and add an `initializeCharts` helper that runs on
  `DOMContentLoaded` and every `htmx:afterSwap` event.
- Widget templates embed the JSON chart config in a lightweight
  `<script type="application/json">` tag. The TypeScript helper parses the
  payload and mounts the chart, destroying any previous instance bound to that
  canvas.
- The global bundle owns other progressive enhancements (tooltips, HTMX
  cleanup) so chart bootstrapping slots neatly into the existing life-cycle.

## Extending the Dashboard

To add a new widget:

1. Subclass `DashboardWidget`, populate the metadata, and implement
   `get_context_data`.
2. Decorate the class with `@register_widget` inside a module that is imported
   by `simplevalidations.dashboard.widgets`.
3. Create a template that extends `dashboard/widgets/base_widget.html`. Use the
   card structure to keep styling consistent.
4. Prefer `dashboard.services.generate_time_series` and
   `build_chart_payload` for line/bar charts.
5. Add tests that cover both the context data and the HTMX response.

To add new time ranges, edit `dashboard.time_ranges` and the select element in
`my_dashboard.html` will render the new option automatically.

## Testing

`simplevalidations/dashboard/tests/test_views.py` exercises the HTMX endpoint
behaviour, org scoping, and aggregation logic. Use the existing factories and
helpers when writing additional cases so test flows mirror production data
relationships.

## Seeding Demo Data

- Use `simplevalidations.tracking.sample_data.seed_sample_tracking_data()` when
  tests need a handful of events without building full validation runs.
- The `seed_tracking_events` management command wraps the helper so local
  development environments can populate meaningful dashboard data:

  ```bash
  uv run -- python manage.py seed_tracking_events --org-slug=my-org --days=14
  ```

  Supply `--runs-per-day`, `--logins-per-day`, or `--no-failures` to tune the
  generated mix. The command reuses existing org/projects/users when possible
  and falls back to lightweight sample instances.

## Follow-up Work

- Expand tracking coverage to include workflow CRUD and submission lifecycle
  events so operators can correlate chart spikes with configuration changes.
- Capture warning-level findings in a future widget so teams can spot noisy
  validators before they fail.
- Evaluate whether we need rollups larger than daily once the 90-day window
  carries more volume; the bucket helper is ready for additional granularities.

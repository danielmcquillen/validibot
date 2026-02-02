# Documentation Strategy

Validibot maintains two complementary documentation sets so that each audience gets the right level of detail:

## 1. User Documentation (`docs/user_docs`)

- **Audience**: Customers and evaluators using Validibot day-to-day
- **Published to**: https://docs.validibot.com
- **Content**: Feature walkthroughs, tutorials, API reference, FAQs

### Local Preview

```bash
# Install dependencies
uv sync --extra dev

# Preview with hot reload
uv run mkdocs serve -f mkdocs.user.yml
# Opens at http://localhost:9001
```

## 2. Developer Documentation (`docs/dev_docs`)

- **Audience**: Engineers and technical partners working directly with the repository
- **Published to**: https://dev.validibot.com
- **Content**: Architecture notes, data models, deployment guides, onboarding for contributors

### Local Preview

```bash
uv run mkdocs serve -f mkdocs.dev.yml
# Opens at http://localhost:9000
```

## Publishing

Documentation is published via the `validibot-marketing` Django app. The MkDocs builds are bundled into the Docker image and served by a subdomain middleware.

### How It Works

1. MkDocs builds documentation here → `docs_build/user/` and `docs_build/dev/`
2. `just docs-sync` (in validibot-marketing) copies builds into Django app
3. Docker build includes docs in the container image
4. Subdomain middleware serves them at `docs.validibot.com` and `dev.validibot.com`

### Publishing Docs

From the `validibot-marketing` repo:

```bash
# Sync docs and deploy (recommended)
just deploy-with-docs

# Or step by step:
just docs-sync    # Build MkDocs and copy to Django app
just deploy       # Build and deploy Docker image
```

See `validibot-marketing/docs/docs-publishing.md` for full details.

## 3. Help Pages (`docs/help_pages`)

- These files are for quick-reference help pages shown directly in the app
- Built to FlatPage objects using a management command (not published via MkDocs)

## MkDocs Configuration

Both MkDocs configurations share the same theme but point to different `docs_dir` folders:

| Config | Docs Dir | Build Output | Dev Port |
|--------|----------|--------------|----------|
| `mkdocs.user.yml` | `docs/user_docs/` | `docs_build/user/` | 9001 |
| `mkdocs.dev.yml` | `docs/dev_docs/` | `docs_build/dev/` | 9000 |

The root `mkdocs.yml` inherits from `mkdocs.dev.yml` as a convenience.

```bash
# Build static sites (for manual inspection)
uv run mkdocs build -f mkdocs.user.yml --clean
uv run mkdocs build -f mkdocs.dev.yml --clean
```

> **Tip**: The `docs_build/` directory is gitignored. Clean it out between builds if you switch audiences.

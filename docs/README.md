# Documentation Strategy

Validibot maintains two complementary documentation sets so that each audience gets the right level of detail:

## 1. User Documentation

- **Audience**: Customers and evaluators using Validibot day-to-day
- **Published to**: https://docs.validibot.com
- **Content**: Feature walkthroughs, tutorials, API reference, FAQs
- **Location**: Lives in the `validibot-marketing` repo (`docs/user_docs/`)

User docs are managed alongside the marketing site because they're customer-facing content. To preview or publish, use the `validibot-marketing` justfile:

```bash
# In validibot-marketing repo
just docs-serve-user   # Preview at http://localhost:9001
just deploy-with-docs  # Build and deploy
```

## 2. Developer Documentation (`docs/dev_docs`)

- **Audience**: Engineers and technical partners working directly with the repository
- **Published to**: https://dev.validibot.com
- **Content**: Architecture notes, data models, deployment guides, onboarding for contributors

### Local Preview

```bash
uv run zensical serve -f mkdocs.dev.yml
# Opens at http://localhost:9000
```

## Publishing

Documentation is published via the `validibot-marketing` Django app. The Zensical builds are bundled into the Docker image and served by a subdomain middleware.

### How It Works

1. User docs are built in the `validibot-marketing` repo
2. Dev docs are built in this repo → `docs_build/dev/`
3. `just docs-sync` (in validibot-marketing) copies both builds into the Django app
4. Docker build includes docs in the container image
5. Subdomain middleware serves them at `docs.validibot.com` and `dev.validibot.com`

### Publishing Docs

From the `validibot-marketing` repo:

```bash
# Sync docs and deploy (recommended)
just deploy-with-docs

# Or step by step:
just docs-sync    # Build docs and copy to Django app
just deploy       # Build and deploy Docker image
```

See `validibot-marketing/docs/docs-publishing.md` for full details.

## 3. Help Pages (`docs/help_pages`)

- These files are for quick-reference help pages shown directly in the app
- Built to FlatPage objects using a management command (not published via Zensical)

## Configuration

Zensical reads the existing `mkdocs.yml` config files natively.

| Config | Docs Dir | Build Output | Dev Port |
|--------|----------|--------------|----------|
| `mkdocs.user.yml` (marketing repo) | `docs/user_docs/` | `docs_build/user/` | 9001 |
| `mkdocs.dev.yml` (this repo) | `docs/dev_docs/` | `docs_build/dev/` | 9000 |

The root `mkdocs.yml` inherits from `mkdocs.dev.yml` as a convenience.

```bash
# Build dev docs static site (for manual inspection)
uv run zensical build -f mkdocs.dev.yml --clean
```

> **Tip**: The `docs_build/` directory is gitignored. Clean it out between builds if you switch audiences.

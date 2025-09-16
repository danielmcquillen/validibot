# Documentation strategy

Roscoe maintains two complementary documentation sets so that each audience gets the right level of detail:

## 1. Public user documentation (`docs/user_docs`)

* Audience: Customers and evaluators using Roscoe day-to-day.
* Publishing flow:
  1. Edit Markdown content under `docs/user_docs/`.
  2. Preview locally with `mkdocs serve -f mkdocs.user.yml`.
  3. Build static assets with `mkdocs build -f mkdocs.user.yml`.
  4. Publish the generated `site/` directory to your public host (e.g., GitHub Pages or Netlify).
* This site focuses on feature walkthroughs, tutorials, and FAQs. Marketing pages should link to the deployed URL.

## 2. Developer documentation (`docs/dev_docs`)

* Audience: Engineers and technical partners working directly with the repository.
* Running locally:
  1. Install dependencies (`pip install -r requirements/docs.txt` or equivalent).
  2. Preview with `mkdocs serve -f mkdocs.dev.yml`.
  3. Build with `mkdocs build -f mkdocs.dev.yml` for offline bundles.
* Content covers architecture notes, data models, and onboarding for contributors.

Both MkDocs configurations share the same theme but point to different `docs_dir` folders. Choose the appropriate config file when running MkDocs:

```bash
# Public user site
mkdocs serve -f mkdocs.user.yml
mkdocs build -f mkdocs.user.yml

# Developer site
mkdocs serve -f mkdocs.dev.yml
mkdocs build -f mkdocs.dev.yml
```

> Tip: Clean out the `site/` directory between builds if you switch audiences to avoid mixing outputs.

# Summary

<!-- What does this PR change, and why? A couple of sentences is plenty. -->

## Checklist

- [ ] Tests added or updated for the behavior this PR changes
- [ ] `pre-commit run --all-files` passes locally

### Docs sync

<!-- Docs drift is found and fixed in painful batch audits — cheaper to
     catch it here. Tick what applies; delete this section for changes
     with no doc-visible surface (pure refactors, test-only changes). -->

- [ ] **Renamed/added/removed a model or field?** Update the matching
      page under `docs/dev_docs/data-model/` (and `overview/` if the
      concept appears there).
- [ ] **Changed a `just` recipe, env var, or settings module?** Update
      `docs/dev_docs/deployment/` and `.envs.example/`.
- [ ] **Added or changed a doctor check (`VBnnn`)?** Update BOTH
      `docs/operations/self-hosting/doctor-check-ids.md` and
      `docs/dev_docs/how-to/doctor-check-ids.md`.
- [ ] **Changed API endpoints, CLI flags, or MCP tools?** Update the
      user docs (`validibot-marketing/docs/user_docs/`) and the CLI
      README where relevant.
- [ ] **User-visible feature?** Consider an entry for the user docs
      "What's New" page and the release notes.

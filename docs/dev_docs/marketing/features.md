# Marketing Feature Pages

The feature pages introduce SimpleValidations’ core capabilities without diving into
implementation specifics. They set expectations for what the platform delivers today
and where it is heading, while keeping copy grounded in workflows we already support.

## Copy themes

- Focus on clarity first: explain how a capability helps a data or validation team work
  faster with more trust.
- Highlight collaboration and auditability—two ideas that run across every feature.
- Keep promises achievable. Mention future directions only when we understand how they
  will land in the product roadmap.

## Page outlines

### Platform overview
- Explains how the workspace unifies rule authoring, automation, and audit trails.
- Emphasises governance, collaboration, and visibility for stakeholders.

### Schema validation
- Describes capturing structural expectations, versioning rules, and surfacing diffs.
- Reinforces collaboration between analysts and engineers when rules evolve.

### Simulation validation
- Covers modelling scenarios with representative data, reviewing results together, and
  promoting trusted configurations into production workflows.

### Certificates
- Positions certificates as shareable evidence that links back to detailed run history.
- Notes typical consumers (auditors, leadership) and what each certificate contains.

### Blockchain
- Frames ledger anchoring as an optional trust signal for high-stakes validations.
- Clarifies that only cryptographic fingerprints leave the platform.

### Integrations
- Covers GitHub and Slack together: launching validations from pull requests, sharing
  results in review threads, and broadcasting actionable updates to chat channels.

## Maintenance tips

- Keep each page using Django translation tags (`trans`/`blocktrans`) for every
  visitor-facing string, favouring short paragraphs over complex layout blocks.
- Revisit the copy as we ship roadmap updates so marketing claims stay aligned with the
  product.
- When you adjust artwork, update the relevant `share_image_path` on each feature view
  so Open Graph and Twitter cards render the correct illustration.
- When introducing new integrations or feature pages, add a short summary here so we
  preserve the storytelling arc across the marketing site.

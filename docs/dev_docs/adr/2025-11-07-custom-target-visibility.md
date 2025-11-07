# ADR 2025-11-07: Catalog Coverage View For Custom Targets

## Status

Proposed

## Context

Manual assertions (BASIC validators) and custom targets on catalog-backed steps
let authors move quickly before every signal is curated. Once those assertions
exist, validator owners need a way to see which targets still lack catalog
definitions and whether a validator is currently allowing custom entries at
all. Today that information is scattered across workflow steps and rulesets, so
clean-up work requires spelunking into individual assertions.

## Decision

Introduce a lightweight “catalog coverage” view that summarizes every custom
target referenced by workflow assertions per validator:

1. Aggregate assertion targets (both catalog-backed and free-form) for each
   validator/ruleset combination.
2. Highlight targets that do not map to an existing catalog entry so validator
   owners can backfill them.
3. Expose whether the validator currently allows custom targets; when the flag
   is disabled, surface blocking assertions so authors know which steps must be
   updated first.

This view will live under the Validator Library so org owners can audit their
validators without visiting each workflow.

## Consequences

- Custom assertion usage becomes transparent, making it obvious which signals
  still need first-class catalog entries.
- Validator owners have a concrete workflow for disabling
  `allow_custom_assertion_targets`: resolve the flagged steps, then flip the
  toggle with confidence.
- Implementation-wise we’ll need a small reporting query (likely materialized
  via SQL view or aggregation) and a UI table showing the slugs, referencing
  workflows, and suggested actions.

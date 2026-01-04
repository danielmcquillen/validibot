# Validibot Product Strategy (2025–2026)

**Status:** Draft  
**Owner:** Daniel McQuillen  
**Last updated:** 2025-12-04

## 1. Purpose

This document defines the product strategy for Validibot for the next 12–24 months.
It exists to stop thrashing, guide feature decisions, and shape how we talk about Validibot
to users, collaborators, and future buyers.

It complements ADRs (which record specific decisions) by describing the
overall direction and the bets we're making.

## 2. Vision

Validibot is a platform for validating complex technical data and models
against rich, containerised logic.

Instead of simple "if field X is empty" checks, Validibot runs deep
validators -- FMUs, EnergyPlus models, simulations, and advanced scripts -- and
turns their results into clear, reusable workflows the whole team can trust.

Long term, Validibot should be the obvious choice when someone asks:
"How do we know this model/config/data is actually good enough to rely on?"

## 3. Target Users (2025–2026)

Primary:

- Technical leads, consultants, and engineers working with:
  - Building performance and energy models (e.g., EnergyPlus)
  - FMUs and other simulation artefacts
  - Complex JSON/XML/CSV configurations for engineering tools

Characteristics:

- They already run simulations or complex checks today.
- They currently rely on scripts, ad-hoc tools, or manual review.
- Bad inputs or broken models are expensive (time, rework, reputation).
- They are willing to pay for reliability, repeatability, and clear audit trails.

Secondary (future):

- Teams in adjacent domains using FMUs or comparable black-box models
  (controls, HVAC, energy systems, etc.) who need validation but aren't
  in the building-performance niche.

## 4. Problem

Today, validating complex technical artefacts is:

- Ad-hoc: scripts scattered across repos, laptops, and old servers.
- Opaque: only one or two people fully understand the checks.
- Hard to share: turning checks into repeatable workflows for non-experts is painful.
- Hard to trust: there's no consistent, auditable record of "what was checked, when, and by which logic."

Generic workflow tools (Zapier, Make, n8n) can orchestrate steps,
but they don't provide deep validation semantics, domain-specific validators,
or credentialing of results.

## 5. Product Definition

Validibot **is**:

- A platform for defining, running, and sharing **validation workflows**
  that can call both:
  - Simple validators (JSON/XML schemas, CEL assertions, basic scripts)
  - Heavy validators (FMUs, EnergyPlus, other simulations in containers)
- A way to **wrap complex logic** (e.g. an FMU) behind a clean interface:
  defined inputs, outputs, and assertions on both.
- A system that can **issue and verify credentials** (badges, VCs)
  proving that a model/data passed specific checks.

Validibot is **not**:

- A generic workflow automation tool like Zapier, Make, or n8n.
- An ETL/ELT pipeline or data warehouse tool.
- A low-code app builder.

## 6. Focus Areas (Validators and Domains)

### 6.1 Core bet: contained complex validators

Core idea: "If the logic fits in a container or engine, Validibot can treat it
as a validator."

For the next 12–24 months, we focus on:

- **FMU-based validators**

  - FMUs as self-contained simulation units.
  - Well-suited for a "sandbox" model where users upload FMUs,
    define signals and assertions, and expose them as validators in workflows.

- **EnergyPlus-based validators**
  - Not a giant market on its own, but:
    - Strong alignment with existing experience and network.
    - Good wedge for early design partners.
    - Excellent storytelling/demo value ("validate your building models before they waste days of sim time").

These are **hero examples**, not the whole product.
The platform should remain general enough to support other containerised validators later.

### 6.2 Positioning versus generic workflow tools

When someone says "why not just use n8n / Zapier / Make?":

- They orchestrate; we **validate deeply.**
- They connect APIs; we wrap simulation engines, models, and heavy logic.
- They send notifications; we generate **evidence** (reports, credentials) that models/data passed real checks.

## 7. Non-Goals (2025–2026)

To avoid dilution:

- We will **not** try to compete as a generic workflow automation platform.
- We will **not** build an integration zoo (hundreds of connectors) just to tick boxes.
- We will **not** optimise for super high-volume, low-value webhook flows.
- We will **not** become a BI tool or dashboard product.

## 8. Product Pillars

1. **Trustable validators**

   - Clear contracts for inputs/outputs.
   - Versioned validator definitions.
   - Reproducible runs with auditable logs.

2. **Expressive assertions**

   - CEL-based assertions over inputs and outputs.
   - Domain-specific helpers where it makes sense (e.g. building metrics).

3. **Composable workflows**

   - Chain multiple validators.
   - Gate later steps on validation results.
   - Reuse workflows across teams and organisations.

4. **Proof of validation**
   - Human-friendly reports.
   - Machine-verifiable credentials (VC 2.0, Open Badges).
   - Optional public verification endpoints.

## 9. 12–18 Month Themes

Theme 1: **Nail the “FMU/E+ as validator” story**

- Implement FMU and EnergyPlus as first-class validator types.
- Build at least one excellent demo workflow for each.
- Secure 2–5 design partners in building/energy modelling.

Theme 2: **Make workflows easy to define and share**

- Solid authoring experience for validation workflows.
- Role-based access (creator, executor, reviewer, viewer).
- Org-level sharing and library concepts.

Theme 3: **Introduce credentials as a differentiator**

- Attach verifiable credentials to successful validation runs.
- Make it trivial to embed "this model has been validated" badges in reports or dashboards.

Theme 4: **Hardening and performance**

- Reasonable limits around runtime, time horizon, and resource use.
- Reliable execution using Google Cloud + containerised jobs.

## 10. Open Questions

- How quickly should we invest beyond building/energy into other FMU-heavy domains?
- Where exactly do we draw the line between "just run my arbitrary code" and "this is a supported validator type"?
- What pricing models make sense for heavy validators (credits, plans, overage)?
- How much "no-code" UX do we need for the first 10 paying customers versus power-user UX?

# Validibot User Guide

Welcome to Validibot, a data validation engine for building energy models and other technical data. This guide covers the everyday tasks you'll encounter: creating workflows, running validations, and interpreting results.

## What is Validibot?

Validibot helps you validate technical data by running it through configurable workflows. Each workflow contains one or more validation steps that check your data against schemas, rules, or simulation-based criteria. Whether you're validating JSON configuration files, XML documents, or EnergyPlus building models, Validibot provides a consistent interface for defining and executing validation rules.

## Quick Start

1. **Sign in** and select your organization from the workspace switcher in the header.
2. **Create a workflow** by clicking "New Workflow" from the Workflows page. Give it a name, assign it to a project, and add at least one validation step.
3. **Launch a validation** by uploading a file or sending data through the API. Validibot runs your data through each step and reports the results.

## Key Concepts

Before diving in, here are the core ideas you'll work with:

- **Organization**: Your workspace. Each organization has its own workflows, projects, and team members.
- **Project**: A way to group related workflows and submissions. Think of it like a folder.
- **Workflow**: An ordered sequence of validation steps. When you submit data, it runs through each step in order.
- **Validator**: The engine that performs a specific type of check (JSON Schema validation, XML Schema validation, EnergyPlus simulation, etc.).
- **Submission**: The file or data payload you want to validate.
- **Validation Run**: A single execution of a workflow against a submission. Each run produces findings that tell you what passed and what failed.

## Documentation Sections

Use the navigation on the left to explore:

- **[Getting Started](getting-started.md)** — Create your first workflow and run a validation.
- **[Workflow Management](workflow-management.md)** — Organize, edit, and maintain your validation workflows.
- **[Running Validations](running-validations.md)** — Launch validations from the UI or API.
- **[Reviewing Results](reviewing-results.md)** — Understand findings, severity levels, and pass/fail outcomes.
- **[Collaboration](collaboration.md)** — Invite teammates and manage roles.
- **[API Reference](api-overview.md)** — Integrate Validibot into your systems.
- **[Troubleshooting](troubleshooting.md)** — Solutions for common issues.
- **[FAQ](faq.md)** — Answers to frequently asked questions.
- **[Glossary](glossary.md)** — Definitions of Validibot terminology.

## Getting Help

If you run into issues or have questions:

- Check the [Troubleshooting](troubleshooting.md) guide for common problems.
- Review the [FAQ](faq.md) for quick answers.
- For Community edition users: Open an issue on [GitHub](https://github.com/validibot/validibot/issues).
- For Pro edition users: Contact support via email (included with your license).

# Validibot User Guide

## One Minute Overview

Validibot is a data validation platform that lets you author "validation workflows" to
check data against rules you define.

"How exactly?" I hear you say.

Easy: you create validation workflows with specific rules about how
to validate data with a nice, friendly authoring feature. Each workflow has an API and a
web-based launch page. You or your users can use then use that workflow API or launch page
to send in data for validation. Validibot returns helpful error messages you defined, or a nice success
message if all is well.

Some of your workflows might do simple kinds of validations, like checking incoming
JSON data against a JSON schema.

Other validations might encapsulate complex validations that need special tools
to perform their checks, like simulations or custom libraries. We call those
"Advanced Validations" ( Validibot has a way of packaging that advanced logic using Docker
and then calling the Docker container as part of a validation workflow ).

A validation workflow might perform other actions, too, like creating a cryptographically signed
certificate to acknowledge a successful validation, or calling a webhook or sending a slack
message.

"Ok, that's all dumb. I could just have our developers create a custom API."

Very true. However, Validibot might be helpful if you want a nice UI and a standardized way of

- defining workflows
- creating new advanced validations
- shielding your users from complex processes
- giving your users their own tool to validate data (and leave you alone)
- tracking and analyzing validations
- performing various actions and integrations

...and have your developers work on something more important to your project.

This guide explains how to use Validot as an admin, author or validation user.
The docs are quick and concise for a world of brains melted by short-form content.

If you want the (not so) gory details of actually setting up and running Validibot, have a
look at our [developer docs](https://dev.validibot.com)

---

## What can you validate?

Validibot works with any structured data. Common use cases:

| Use Case                   | Data Type              | Validators                        |
| -------------------------- | ---------------------- | --------------------------------- |
| **Configuration files**    | JSON, YAML             | JSON Schema, Basic assertions     |
| **API payloads**           | JSON, XML              | JSON Schema, XML Schema           |
| **Data exports**           | CSV, JSON              | Basic assertions, AI validation   |
| **Compliance checks**      | Any format             | Custom rules with CEL expressions |
| **FMU-based simulation**   | JSON, YAML, XML        | FMU simulation                    |
| **Building energy models** | EnergyPlus IDF, epJSON | EnergyPlus                        |

---

## Super quick start

Get your first validation running in under 5 minutes. (This assumes you've made someone
set up a Validibot instance for you.) :

### 1. Create a workflow

A validation workflow is a sequence of validation steps. Each step runs a validator
that checks specific aspects of your data.

Log in to validibot and create a new workflow.

```
Workflows → New Workflow → Add a name → Select allowed file types
```

### 2. Add validation steps

Add one or more validators to your workflow:

- **JSON Schema** — Validates structure against a JSON Schema
- **XML Schema** — Validates XML against an XSD
- **Basic** — Custom rules using CEL expressions
- **AI** — Natural language validation rules

### 3. Activate and run

Set the workflow to **Active**, then click **Launch** to upload a file.

Add some sample data to the launch form and launch the validation. Validibot runs your data through each step and shows you exactly what passed and what failed. What's up.

**[→ Full getting started guide](getting-started.md)**

---

## Core concepts

Validibot is built around a few key ideas:

**Validation Workflows** contain ordered validation steps. When you submit data, Validibot runs it
through each step sequentially.

**Validators** are the engines that check your data. Built-in "simple" validators include JSON Schema, XML Schema, and AI validators. Advanced validators like EnergyPlus run simulations against your models (more on "simple" vs. "advanced" later...).

**Findings** are the issues discovered during validation. Each finding has a severity (error, warning, info) and tells you exactly what's wrong and where.

**Organizations** are workspaces that contain your workflows, projects, and team members. You can belong to multiple organizations.

**[→ Full glossary](glossary.md)**

---

## Documentation

| Guide                                             | Description                                        |
| ------------------------------------------------- | -------------------------------------------------- |
| [**Getting Started**](getting-started.md)         | Create your first workflow and run a validation    |
| [**Workflow Management**](workflow-management.md) | Create, configure, and manage validation workflows |
| [**Running Validations**](running-validations.md) | Submit data via the UI or API                      |
| [**Reviewing Results**](reviewing-results.md)     | Understand findings and pass/fail outcomes         |
| [**API Overview**](api-overview.md)               | Integrate Validibot into your systems              |
| [**Collaboration**](collaboration.md)             | Invite teammates and manage access                 |

---

## Get help

**Something not working?** Check [Troubleshooting](troubleshooting.md) for common issues.

**Have a question?** See the [FAQ](faq.md) or:

- **Community edition**: [Open a GitHub issue](https://github.com/danielmcquillen/validibot/issues)
- **Pro edition**: Email support (included with your license)

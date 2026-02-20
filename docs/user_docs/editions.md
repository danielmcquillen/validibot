# Validibot Editions

Validibot is available in two editions: **Community** (free, open source) and **Pro** (commercial license).

## Philosophy

**Community is for humans. Pro is for machines.**

The Community edition gives you full access to Validibot's validation capabilities, including advanced validators. Run simple validations(JSON Schema, XML Schema, custom CEL statements, etc.) and advanced validations (FMU, EnergyPlus, etc.), explore the results, prove the tool works for your use case. There's no restrictions on what you can validate.

The Pro edition adds everything you need to operationalize validation in your engineering workflow: CI/CD integration, machine-readable outputs, parallel execution, reporting, and commercial support.

## Feature Comparison

| Feature                                                       | Community |    Pro     |
| ------------------------------------------------------------- | :-------: | :--------: |
| **Validators**                                                |           |            |
| Basic validators (schema, syntax, structure)                  |     ✓     |     ✓      |
| Advanced validators (FMU, simulation-based)                   |     ✓     |     ✓      |
| Custom validator development                                  |     ✓     |     ✓      |
| **Usage**                                                     |           |            |
| CLI usage                                                     |     ✓     |     ✓      |
| Run locally / self-host                                       |     ✓     |     ✓      |
| CI/CD environments (GitHub Actions, GitLab CI, Jenkins, etc.) |           |     ✓      |
| API access                                                    |           |     ✓      |
| **Output Formats**                                            |           |            |
| Basic text output (pass/fail, summary)                        |     ✓     |     ✓      |
| JUnit XML (CI test results)                                   |           |     ✓      |
| SARIF (GitHub code scanning)                                  |           |     ✓      |
| JSON (custom integrations)                                    |           |     ✓      |
| Rich HTML/PDF reports                                         |           |     ✓      |
| **Performance**                                               |           |            |
| Sequential validation                                         |     ✓     |     ✓      |
| Parallel execution                                            |           |     ✓      |
| Incremental validation (cache unchanged files)                |           |     ✓      |
| **Workflow Integration**                                      |           |            |
| Baseline comparison (fail only on new issues)                 |           |     ✓      |
| Configurable exit codes                                       |           |     ✓      |
| PR/MR comment integration                                     |           |     ✓      |
| Metrics export (Prometheus, StatsD, OpenTelemetry)            |           |     ✓      |
| **License & Support**                                         |           |            |
| License                                                       | AGPL-3.0  | Commercial |
| Community support (GitHub Issues)                             |     ✓     |     ✓      |
| Email support                                                 |           |     ✓      |
| Priority support                                              |           | Enterprise |

## Community Edition

The Community edition is licensed under [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.en.html). You can use it freely for any purpose, but modifications must be released under the same license.

**Install from PyPI:**

```bash
pip install validibot
```

**What you can do:**

- Validate FMUs, configuration files, and other technical artifacts
- Use all validators, including advanced simulation-based validators
- Build and test custom validators
- Integrate into your own tools and scripts (under AGPL terms)

## Pro Edition

Pro is for teams who want to integrate Validibot into their CI/CD pipelines and engineering workflows.

**Pricing:** $995/year per organization

**What you get:**

- **CI/CD integration** — Run validations in GitHub Actions, GitLab CI, Jenkins, Azure DevOps, and other CI environments
- **Machine-readable outputs** — JUnit XML for CI dashboards, SARIF for GitHub code scanning, JSON for custom tooling
- **Rich reports** — Generate HTML and PDF validation reports for stakeholders
- **Performance** — Parallel execution and incremental validation for faster pipelines
- **Workflow features** — Baseline comparison, configurable exit codes, PR comments
- **Observability** — Export metrics to Prometheus, StatsD, or OpenTelemetry
- **Commercial license** — Use Validibot without AGPL obligations
- **Email support** — Direct access to the maintainers

**Purchase:** [validibot.com/pricing](https://validibot.com/pricing)

### Setting up Validibot Pro

After purchasing, you'll receive credentials (email and API key) on your [License Dashboard](https://validibot.com/dashboard/). Follow these steps to install and activate Pro features.

#### Step 1: Configure pip credentials

Choose one of the following methods to authenticate with the private package server.

=== "pip config (recommended)"

    Run this once to save your credentials globally:

    ```bash
    pip config set global.extra-index-url https://YOUR_EMAIL:YOUR_API_KEY@validibot.com/simple/
    ```

=== "Environment variables"

    Set these in your shell profile or CI environment:

    ```bash
    export VALIDIBOT_USER="your-email@example.com"
    export VALIDIBOT_PASS="your-api-key"
    ```

    Then reference them in your `requirements.txt`:

    ```
    --extra-index-url https://${VALIDIBOT_USER}:${VALIDIBOT_PASS}@validibot.com/simple/
    validibot-pro>=1.0.0
    ```

!!! tip
    Your credentials are available on your [License Dashboard](https://validibot.com/dashboard/) with copy-to-clipboard buttons.

#### Step 2: Install the package

```bash
pip install validibot-pro
```

#### Step 3: Add to INSTALLED_APPS

In your Django settings file (typically `config/settings/base.py`), add `validibot_pro` to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    # ... existing apps ...
    "validibot_pro",
]
```

#### Step 4: Restart Validibot

Restart your Validibot instance. Pro features will be available immediately — no license keys or activation steps required. Installing the package is the license.

!!! note
    You can verify Pro is active by checking the edition badge in the Validibot footer or by running `validibot --version`.

## Frequently Asked Questions

### Can I use Community edition in CI/CD?

The Community edition detects CI environments and will exit with an error. This is how we keep the project sustainable while keeping the core validation engine free.

### Can I evaluate Pro before purchasing?

Yes. The Community edition includes all validators—you can prove the validation logic works for your use case locally. Pro adds the operational features for CI/CD integration.

If you need to evaluate the full Pro feature set, contact us for a trial license.

### What if my Pro license expires?

You can continue using the version you have installed, but you won't be able to download updates or reinstall. Renew your license to restore access.

### Do you offer Enterprise pricing?

Yes. For larger organizations needing distributed execution, LDAP integration, or custom SLAs, contact us at enterprise@mcquilleninteractive.com.

### I have AGPL compliance questions

If your legal team has concerns about AGPL, the Pro commercial license removes those obligations. This is a common reason organizations choose Pro even before they need CI/CD features.

## Support

- **Community:** [GitHub Issues](https://github.com/danielmcquillen/validibot/issues)
- **Pro:** Email support included with your license
- **Documentation:** [https://validibot.com/resources/docs/](https://validibot.com/resources/docs/)

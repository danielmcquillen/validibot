# Validibot Platform Overview

Validibot is a comprehensive data validation orchestration platform designed to help organizations build, manage, and execute robust validation workflows at scale.

## What is Validibot?

Validibot transforms the traditionally fragmented and error-prone process of data validation into a systematic, reliable, and collaborative workflow. Instead of writing one-off scripts or maintaining complex validation codebases, teams can build reusable validation pipelines that enforce data quality standards consistently across their organization.

### The Problem We Solve

Modern applications handle increasingly complex data from multiple sources - APIs, file uploads, user inputs, third-party integrations. Traditional approaches to validation often suffer from:

- **Inconsistency**: Different teams implementing different validation logic for similar data
- **Fragmentation**: Validation scattered across multiple codebases, scripts, and manual processes
- **Poor Visibility**: No centralized view of validation results, trends, or failure patterns
- **Maintenance Burden**: Validation logic tightly coupled to application code, making updates risky
- **Limited Reusability**: Validation logic written for one use case can't easily be applied elsewhere
- **Collaboration Challenges**: Domain experts can't easily contribute to or review validation rules

### The Validibot Solution

Validibot provides a platform where validation logic is:

- **Centralized**: All validation workflows live in one place with proper versioning
- **Reusable**: Create workflows once, use them across multiple applications and contexts
- **Collaborative**: Domain experts can review and contribute to validation rules without code changes
- **Observable**: Rich reporting and analytics on validation results and trends
- **Maintainable**: Updates to validation logic are isolated and can be tested independently
- **Scalable**: Built-in async processing handles large volumes and long-running validations

## Core Concepts

### Organizations

Organizations are the top-level container for all resources in Validibot. They provide:

- **Isolation**: Each organization's workflows, rulesets, and data are completely separate
- **Access Control**: Users belong to organizations and have specific roles within them
- **Billing Context**: Usage and billing are tracked at the organization level
- **Collaboration Boundary**: Teams collaborate within their organization's workspace

### Workflows

Workflows are the heart of Validibot - reusable, versioned definitions of validation processes. Each workflow:

- **Defines a Process**: An ordered sequence of validation steps to execute
- **Owns Configuration**: Specifies which validators to use and how they should behave
- **Provides Versioning**: Multiple versions can coexist, allowing gradual migration
- **Enables Reuse**: The same workflow can validate different submissions over time

Think of workflows as "validation recipes" that can be applied to various data sources.

### Submissions

Submissions represent the actual content being validated. They can be:

- **Inline Text**: JSON, XML, CSV, or other text content passed directly via API
- **File Uploads**: Documents uploaded through the UI or API endpoints
- **Multi-part Data**: Complex submissions with multiple files or data elements

Submissions are immutable once created, ensuring validation results remain reproducible.

### Validation Runs

A Validation Run represents one execution of a specific workflow against a specific submission. Each run:

- **Captures Context**: When it ran, who triggered it, which workflow version was used
- **Tracks Execution**: Status, timing, and progression through workflow steps
- **Stores Results**: Issues found, statistics, and artifacts produced during validation
- **Provides Traceability**: Complete audit trail for compliance and debugging

### Validation Steps and Engines

Each workflow consists of one or more validation steps. Each step:

- **Uses a Validator**: A specific type of validation engine (JSON Schema, XML Schema, custom logic)
- **Applies Rules**: Optional ruleset that defines the specific validation criteria
- **Produces Results**: Issues, warnings, and metadata from the validation process
- **Can Be Reordered**: Steps execute in sequence and can be rearranged as needed

Validibot ships with built-in validation engines for common formats and supports custom validators for specialized use cases.

### Rulesets

Rulesets contain the actual validation rules that define what constitutes valid data:

- **JSON Schema**: For validating JSON structure and content
- **XML Schema (XSD)**: For validating XML documents
- **Custom Validator**: Organization-specific validation logic
- **Shared vs Private**: Rulesets can be shared across the organization or kept private to specific workflows

### Validation Findings

Findings are the normalized output from validation steps - the issues, warnings, and information discovered during validation:

- **Severity Levels**: ERROR (validation failure), WARNING (potential issue), INFO (contextual information)
- **Path Information**: Precise location of issues within the validated content
- **Structured Data**: Consistent format enables aggregation, filtering, and trend analysis
- **Rich Context**: Additional metadata to help developers understand and fix issues

## Key Benefits

### For Development Teams

- **Reduced Maintenance**: Validation logic lives outside application code
- **Consistent Standards**: Organization-wide validation standards are automatically enforced
- **Faster Development**: Reuse existing workflows instead of writing validation from scratch
- **Better Testing**: Test validation logic independently from application logic
- **Rich Debugging**: Detailed validation results help identify issues quickly

### For Data Teams

- **Quality Visibility**: Dashboard views of data quality trends and issues
- **Proactive Monitoring**: Set up alerts when validation failure rates exceed thresholds
- **Historical Analysis**: Track data quality improvements over time
- **Root Cause Analysis**: Drill down into specific validation failures to identify systemic issues

### for DevOps Teams

- **Scalable Processing**: Async validation processing handles large volumes
- **API-First Design**: Easy integration into CI/CD pipelines and automated workflows
- **Monitoring Integration**: Rich metrics and logging for operational visibility
- **Deployment Independence**: Update validation logic without application deployments

### For Compliance Teams

- **Audit Trails**: Complete history of what was validated, when, and by whom
- **Policy Enforcement**: Ensure data handling meets regulatory requirements
- **Documentation**: Workflows serve as living documentation of validation policies
- **Version Control**: Track changes to validation rules over time

## Integration Patterns

### API Integration

The REST API enables integration with any system that can make HTTP requests:

- **Workflow Execution**: POST data to workflow endpoints to trigger validation
- **Results Polling**: Check validation status and retrieve results
- **Webhook Notifications**: Receive callbacks when validations complete
- **Batch Processing**: Submit multiple files or datasets for validation

### CI/CD Integration

Validibot fits naturally into continuous integration pipelines:

- **Pre-deployment Validation**: Validate configuration files before deployment
- **Data Migration Validation**: Ensure migrated data meets quality standards
- **API Contract Testing**: Validate API responses against expected schemas
- **Configuration Validation**: Check deployment configs against organizational standards

### Application Integration

Applications can leverage Validibot for runtime validation:

- **User Upload Validation**: Validate files uploaded by users before processing
- **API Gateway Integration**: Validate incoming requests at the gateway level
- **Background Job Processing**: Validate large datasets asynchronously
- **Data Import Validation**: Check imported data before committing to databases

## Architecture Principles

Validibot is built on several key architectural principles:

### Separation of Concerns

Validation logic is completely separated from business logic, making both easier to maintain and test.

### Extensibility

The validation engine registry allows new types of validators to be added without core platform changes.

### Scalability

Asynchronous processing via Celery enables the platform to handle large validation workloads.

### Auditability

Every validation run is logged with complete context, providing full traceability.

### Multi-tenancy

Organization-based isolation ensures secure, scalable multi-tenant operation.

## Technology Stack

- **Backend**: Django 5.2+ with Django REST Framework
- **Database**: PostgreSQL with JSON field support
- **Task Queue**: Celery with Redis/RabbitMQ
- **Validation Engines**: Pluggable architecture supporting JSON Schema, XML Schema, and custom validators
- **Authentication**: Django's built-in auth with organization-based access control
- **Storage**: Configurable file storage (local, S3, etc.) for submissions and artifacts
- **Frontend**: Modern Django templates with Bootstrap 5 and HTMx.
- **API**: RESTful API with OpenAPI documentation

## Working Agreements for Developers

- Prefer obvious, readable solutions over clever tricks; when a shortcut is unavoidable, add a brief doc comment pointing back to the relevant section in these developer docs.
- Keep workflow, validation, and submission objects aligned (org, project, and user) to avoid cross-tenant data leaks.
- When editing service layers, capture the request/response flow in docs/dev_docs so future you can reload the narrative quickly.
- Update the developer documentation whenever you introduce a new background job, validation engine, or workflow capability; the docs are treated as the project's shared memory.

## Next Steps

To get started with Validibot:

1. **Read the detailed workflow documentation**: [How It Works](how_it_works.md)
2. **Explore the data model**: [Data Model Overview](../data-model/index.md)
3. **Try the API**: [Using a Workflow via API](../how-to/use-workflow.md)

For ongoing updates and architectural decisions, see the development documentation in this folder.

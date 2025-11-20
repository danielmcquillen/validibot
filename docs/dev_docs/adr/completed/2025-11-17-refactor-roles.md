# ADR-2025-11-17: Refactor Roles

**Date:** 2025-11-17
**Status:** Accepted
**Owners:** Platform / Validations

## Context

Refactor how roles are used throught the application, based on the following grid:

| Role            | Can see workflows | Can edit workflows | Can execute | Can see results for…        | Other                     |
| --------------- | ----------------- | ------------------ | ----------- | --------------------------- | ------------------------- |
| OWNER           | All               | All                | All         | All runs in org             | Billing, members, etc.    |
| ADMIN (if used) | All               | All                | All         | All runs in org             | Org-level management      |
| AUTHOR          | All               | All                | All         | All runs in org             | Power user                |
| EXECUTOR        | All               | No                 | Yes         | **Only runs they launched** | No access to others’ data |
| RESULTS_VIEWER  | No                | No                 | No          | All                         | “Analyst / reviewer”      |
| WORKFLOW_VIEWER | All               | No                 | No          | No                          | Pure read-only            |

All permissions are scoped to an organization and apply only to that organization, and must be defined in each organization where the role should be granted.

An important line is:
EXECUTOR → can launch any allowed workflow, but only sees the results of the runs they kicked off.

## Implementation

Refactor entire codebase to make sure the above Role definitions are respected.
Make sure tests are complete.

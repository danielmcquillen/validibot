# ADR-2015-11-16-CEL-Implementation: Fully implement the CEL feature

## Status

Proposed (2025-11-16)

## Background

As part of an Workflow definition, the author may have defined Assertion instances using CEL syntax to perform basic expressions on input signals, output signals and derived signals.

This processing should happen as part of the Validator engine. As every validator instance may have these kinds of expressions, the feature should be implemented in a way that makes it fundamental to the engine code structure.

# Contributing to Validibot

Thank you for your interest in contributing to Validibot! This document outlines
the process for contributing to this project.

## Project Status

Validibot is under active development. We follow semver and the API will
stabilize for v1.0, but until then breaking changes to APIs, database schemas,
and configuration are possible between minor versions. If you're building on
Validibot, watch the repo or [join the discussion](https://github.com/danielmcquillen/validibot/discussions) to stay updated.

## Copyright and Licensing

By submitting a Pull Request to this project, you disavow any rights or claims
to any changes submitted and assign the copyright of those changes to
McQuillen Interactive Pty. Ltd.

If you cannot or do not want to reassign those rights (for example, your
employer retains intellectual property rights for your work), you should not
submit a Pull Request. Instead, please open an issue describing the change you'd
like to see so that someone else can implement it.

This assignment allows us to maintain Validibot as open source (AGPL-3.0) while
also offering commercial licenses (Validibot Pro and Enterprise) that fund
ongoing development.

## Contribution Guidelines

### Reporting Issues

- Search existing issues before creating a new one
- Include steps to reproduce for bugs
- Include your environment details (Python version, OS, etc.)

### Pull Requests

1. Fork the repository
2. Create a feature branch from `main`
3. Make your changes
4. Ensure tests pass: `just test`
5. Ensure code is formatted: `uv run ruff check`
6. Submit a pull request

### Code Style

- Follow PEP 8 for Python code
- Use type hints where appropriate
- Write docstrings for public functions and classes
- Keep commits focused and atomic

### Testing

- Add tests for new features
- Ensure existing tests pass
- Aim for good test coverage

## Questions?

If you have questions about contributing, feel free to:

- Open a discussion on GitHub
- Email us at contributing@mcquilleninteractive.com

Thank you for contributing!

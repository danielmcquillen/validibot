# Contributing to Validibot

Thank you for your interest in contributing to Validibot! This document outlines
the process for contributing to this project.

## Developer Certificate of Origin (DCO)

This project uses the Developer Certificate of Origin (DCO) to ensure that
contributors have the right to submit their contributions. The DCO is a
lightweight mechanism that doesn't require submission of a formal contributor
license agreement.

By contributing to this project, you agree to the DCO, which you can read in
full in the [DCO](DCO) file. In short, it certifies that:

- You have the right to submit the contribution
- You understand the contribution is public and will be maintained indefinitely
- You grant the project the right to use your contribution under the project's
  license (AGPL-3.0 for open source use, or commercial license)

### How to Sign Off

You must sign off on every commit you contribute. This certifies that you agree
to the DCO.

**Using the command line:**

```bash
git commit -s -m "Your commit message"
```

The `-s` flag adds a `Signed-off-by` line to your commit message:

```
Your commit message

Signed-off-by: Your Name <your.email@example.com>
```

**Configure git to sign off by default (optional):**

Add an alias to make signing off easier:

```bash
git config --global alias.ci 'commit -s'
```

### Fixing Unsigned Commits

If you've already made commits without signing off, you can amend them:

**For the most recent commit:**

```bash
git commit --amend -s --no-edit
```

**For multiple commits:**

```bash
git rebase HEAD~n --signoff
```

Where `n` is the number of commits to sign off.

## Contribution Guidelines

### Reporting Issues

- Search existing issues before creating a new one
- Include steps to reproduce for bugs
- Include your environment details (Python version, OS, etc.)

### Pull Requests

1. Fork the repository
2. Create a feature branch from `main`
3. Make your changes with signed-off commits
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

## Licensing

By contributing to Validibot, you agree that your contributions will be licensed
under:

- **AGPL-3.0** for open source use (see [LICENSE](LICENSE))
- Your contributions may also be included in commercial versions (Validibot Pro
  and Enterprise), which are distributed under separate commercial license terms

This dual-licensing model allows us to offer Validibot as open source while also
funding development through commercial licenses.

## Questions?

If you have questions about contributing, feel free to:

- Open a discussion on GitHub
- Email us at contributing@validibot.com

Thank you for contributing!

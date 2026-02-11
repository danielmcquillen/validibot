# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Validibot, please report it responsibly by emailing **security@mcquilleninteractive.com**. Do not open a public GitHub issue.

Please include a description of the vulnerability and steps to reproduce it.

## What to Expect

Validibot is maintained by a small team (me). I'll do my best to acknowledge reports and work toward a fix, but I can't guarantee specific response times. Critical issues will be prioritized.

If you'd like to be credited in the release notes when a fix ships, let me know in your report.

## Scope

Security reports are welcome for:

- The Validibot Django web application (this repository)
- The REST API
- Authentication and authorization
- Cryptographic operations (credential signing, JWKS)

For vulnerabilities in third-party dependencies, please report those to the upstream project directly.

## Disclaimer

This software is provided "as is", without warranty of any kind. See the [LICENSE](LICENSE) file for full terms. Security fixes are provided on a best-effort basis.

## Security Best Practices for Deployment

When deploying Validibot in production:

1. **Always use HTTPS** in production
2. **Set a strong `DJANGO_SECRET_KEY`** - generate one with:
   ```bash
   python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
   ```
3. **Restrict `DJANGO_ALLOWED_HOSTS`** to your specific domain(s)
4. **Use environment variables** or a secrets manager for all credentials
5. **Never commit `.envs/` files** to version control (they are gitignored by default)
6. **Keep dependencies updated** - run `uv lock --upgrade` regularly

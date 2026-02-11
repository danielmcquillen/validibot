# Validibot Licensing FAQ

This FAQ answers common questions about Validibot licensing. For the complete
legal terms, see the [LICENSE](LICENSE) file and the Commercial License
Agreement provided with your purchase.

---

## General Questions

### What license is Validibot available under?

Validibot is available under a dual license:

1. **AGPL-3.0** (open source) - Free to use with copyleft requirements
2. **Commercial License** (Pro/Enterprise) - Removes AGPL obligations

### What's included in the free Community edition?

Everything functional. The Community edition includes:

- All validators (Basic, JSON Schema, XML Schema, AI, EnergyPlus, FMI)
- Full API access
- CLI tool (validibot-cli)
- Workflows and submissions
- Single-organization support
- Docker Compose for self-hosting

The only limitations are AGPL compliance requirements and lack of commercial
support.

### Do I need a commercial license to use Validibot in CI/CD pipelines?

No. The CLI is free to use anywhere, including CI/CD pipelines, automated
testing, and batch processing. You only need a commercial license if you want
to avoid AGPL obligations or need Pro/Enterprise features.

---

## AGPL-3.0 (Open Source License)

### What does AGPL-3.0 require?

If you modify Validibot and make it available over a network (e.g., as a web
service), you must:

1. Make your complete source code available to users
2. License your modifications under AGPL-3.0
3. Preserve all copyright notices

### Can I use Validibot internally without sharing source code?

Yes, if you're using unmodified Validibot purely internally and not exposing
it to external users over a network, you don't need to share anything.

### What counts as a "modification"?

Changes to Validibot's source code. Configuration, using the API, or writing
integrations that call Validibot are generally not modifications. Custom
validators added via the plugin system may or may not be modifications
depending on how they're integrated - when in doubt, contact us.

### Can I use Validibot as part of a proprietary product?

Not under AGPL-3.0. If you want to distribute Validibot as part of a
proprietary product or offer it as a service without AGPL compliance, you need
a Commercial License.

---

## Commercial Licenses (Pro/Enterprise)

### What's the difference between Pro and Enterprise?

| Feature                    | Pro | Enterprise |
| -------------------------- | --- | ---------- |
| AGPL obligations removed   | Yes | Yes        |
| Priority support           | Yes | Yes        |
| Multi-organization support | Yes | Yes        |
| Advanced analytics         | Yes | Yes        |
| SSO (LDAP, SAML, OAuth)    | No  | Yes        |
| Guest user management      | No  | Yes        |
| Team management (RBAC)     | No  | Yes        |
| Dedicated support channel  | No  | Yes        |
| Source code escrow option  | No  | Yes        |

### How are commercial licenses priced?

Visit [validibot.com/pricing](https://validibot.com/pricing) for current
pricing. Licenses are annual subscriptions.

### Can I try before I buy?

Yes. You can evaluate the Community edition for free. All features work
identically - commercial tiers add team/organization features and remove AGPL
requirements.

### What happens if I don't renew?

Commercial features (multi-org, SSO, etc.) are disabled, but your installation
continues to work with Community features. Your data remains intact. Enterprise
customers get a 60-day transition period.

---

## Deployment & Infrastructure

### Do you host Validibot for me?

No. Validibot is designed for your own infrastructure. You deploy it on your own
infrastructure. We don't have access to your installation or data.

### Can I run Validibot in my own cloud (AWS, GCP, Azure)?

Yes. Validibot runs anywhere Docker runs. We provide Docker Compose
configurations for easy deployment.

### What about Kubernetes?

Validibot can run on Kubernetes. Documentation and Helm charts are available
in the repository.

### Who is responsible for security and backups?

You are. Since you host Validibot, you're responsible for:

- Infrastructure security
- Network configuration
- Applying updates and patches
- Backups and disaster recovery
- Compliance with data protection laws

We provide the software; you provide the infrastructure and operations.

### Do you have access to my data?

No. We don't host, operate, or have access to your installation. Your data
stays on your infrastructure.

---

## Users & Organizations

### What is a "user" for licensing purposes?

A user is anyone with an account in your Validibot installation. This includes
active and inactive accounts.

### Can my contractors use our installation?

Yes. Anyone you authorize can use your installation. Your commercial license
covers your organization, including employees and contractors working on your
behalf.

### Can I run Validibot for multiple unrelated companies?

Under Pro/Enterprise licenses, you cannot use Validibot to provide managed
services to unaffiliated third parties where the primary value is access to
Validibot itself. If you want to operate Validibot for multiple separate
organizations, contact us about reseller arrangements.

### Can I run dev/staging/production environments?

Yes. Your commercial license covers unlimited installations within your
organization, including development, staging, testing, and production
environments.

---

## Support

### What support is included?

| Tier       | Support                                      |
| ---------- | -------------------------------------------- |
| Community  | Community forums, GitHub issues              |
| Pro        | Priority email support, knowledge base       |
| Enterprise | Dedicated support channel, quarterly reviews |

### What are the support response times?

Response times are targets, not guarantees:

| Severity                        | Pro             | Enterprise       |
| ------------------------------- | --------------- | ---------------- |
| Critical (system down)          | Best effort     | 4 business hours |
| High (major feature impaired)   | Best effort     | 8 business hours |
| Medium (minor feature impaired) | 2 business days | 2 business days  |
| Low (general questions)         | 5 business days | 5 business days  |

### What if I need guaranteed SLAs?

Contact us about custom Enterprise agreements with contractual SLAs.

---

## Legal & Compliance

### Can I get source code escrow?

Yes, for Enterprise customers. At your expense, we'll participate in an escrow
arrangement that releases source code if we go out of business or materially
breach the agreement.

### What if your company is acquired?

Our commercial license allows assignment to acquirers. Your license would
transfer to the new owner and remain valid.

### What jurisdiction governs the license?

New South Wales, Australia. Disputes are resolved through ACICA arbitration
in Sydney.

### Is there a liability cap?

Yes. For both Pro and Enterprise: fees paid in the 12 months preceding the claim.

---

## Common Scenarios

### "I want to use Validibot internally at my company"

- **Unmodified, internal only**: Community edition (AGPL) is fine
- **Modified or external users**: Commercial license recommended
- **Need multi-org or SSO**: Pro or Enterprise required

### "I'm building a product that integrates with Validibot"

- **Your product calls Validibot's API**: Generally fine under AGPL
- **Your product embeds/bundles Validibot**: Commercial license required
- **Your product is proprietary SaaS**: Commercial license required

### "I'm a consultant implementing Validibot for clients"

- **Each client hosts their own instance**: Each client needs their own license
- **You want to host for multiple clients**: Contact us about reseller options

### "I'm an open source project"

You can use Validibot under AGPL-3.0 and your users can use AGPL-3.0 too. If
you have questions about compatibility with your license, contact us.

---

## Still Have Questions?

- **Licensing questions**: licensing@mcquilleninteractive.com
- **Sales inquiries**: sales@mcquilleninteractive.com
- **Technical questions**: GitHub issues or community forums

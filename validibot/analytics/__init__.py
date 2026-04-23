"""Advanced analytics app — Pro-gated reporting dashboards.

Bare-bones today: a single placeholder page. Future reports
(run-throughput trends, validator error-rate heatmaps, quota
consumption forecasts) land here without needing a new app or flag.

**Placement rationale.** All business logic lives in community
(``validibot/``) per the open-core architecture. The views are gated
at the mixin level with ``FeatureRequiredMixin(ADVANCED_ANALYTICS)``,
so community-only deployments 404 every URL in this app. Pro
deployments that advertise ``ADVANCED_ANALYTICS`` see the dashboard.

**Why not reuse the audit log's feature flag?** The ADR-2026-04-16
observability taxonomy treats product analytics (Pillar 2, this app)
and the audit log (Pillar 3) as distinct concerns — different
models, retention, consumers. Keeping the flags separate lets a
future tier split ship one without the other.
"""

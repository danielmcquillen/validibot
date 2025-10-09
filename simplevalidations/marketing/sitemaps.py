from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class MarketingStaticViewSitemap(Sitemap):
    priority_map = {
        "marketing:home": 0.9,
        "marketing:features": 0.7,
        "marketing:pricing": 0.7,
    }
    changefreq_map = {
        "marketing:home": "weekly",
        "marketing:features": "monthly",
        "marketing:pricing": "monthly",
        "marketing:resources": "weekly",
        "marketing:faq": "monthly",
        "marketing:status": "daily",
        "marketing:resources_changelog": "weekly",
    }

    def items(self):
        return [
            "marketing:home",
            "marketing:about",
            "marketing:features",
            "marketing:features_overview",
            "marketing:features_schema_validation",
            "marketing:features_simulation_validation",
            "marketing:features_certificates",
            "marketing:features_blockchain",
            "marketing:features_integrations",
            "marketing:pricing",
            "marketing:pricing_starter",
            "marketing:pricing_growth",
            "marketing:pricing_enterprise",
            "marketing:resources",
            "marketing:resources_docs",
            "marketing:resources_videos",
            "marketing:resources_changelog",
            "marketing:faq",
            "marketing:support",
            "marketing:contact",
            "marketing:help_center",
            "marketing:status",
            "marketing:terms",
            "marketing:privacy",
        ]

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        return self.priority_map.get(item, 0.6)

    def changefreq(self, item):
        return self.changefreq_map.get(item, "monthly")

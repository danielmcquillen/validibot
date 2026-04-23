"""Production settings for self-hosted Validibot Pro.

Extends ``config.settings.production`` with the single change needed
to activate Pro: adding ``validibot_pro`` to ``INSTALLED_APPS``.

Installing the wheel (via ``VALIDIBOT_COMMERCIAL_PACKAGE`` build arg
in the production Dockerfile) makes the package importable, but Django
only runs its ``AppConfig.ready()`` and imports its ``__init__.py``
— where ``validibot.core.license.set_license(PRO_LICENSE)`` lives —
when the app is in ``INSTALLED_APPS``. This settings module does that
final step for self-hosted Docker Compose deployments.

Pair with ``DJANGO_SETTINGS_MODULE=config.settings.production_pro``
in the deployment's env file. The hosted cloud offering uses
``validibot_cloud.settings.cloud`` instead, which layers cloud apps
on top and already includes ``validibot_pro`` in its own common-cloud
app list.
"""

from .production import *  # noqa: F403
from .production import INSTALLED_APPS as _BASE_INSTALLED_APPS

INSTALLED_APPS = [*_BASE_INSTALLED_APPS]
if "validibot_pro" not in INSTALLED_APPS:
    # Append rather than insert so Pro-registered URLs and feature
    # registrations run after community bootstraps. The community
    # codebase never imports validibot_pro directly — it reads the
    # registered License via validibot.core.license.get_license().
    INSTALLED_APPS.append("validibot_pro")

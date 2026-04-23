"""Local development settings for community + validibot-pro.

Extends ``config.settings.local`` with the single change needed to
activate Pro: adding ``validibot_pro`` to ``INSTALLED_APPS``.

Django only runs a commercial package's ``AppConfig.ready()`` — and
imports its ``__init__.py`` (which calls
``validibot.core.license.set_license(PRO_LICENSE)``) — when the app is
in ``INSTALLED_APPS``. Installing the wheel alone is not enough. This
settings module does that final step for the docker-compose local-pro
stack, the host-run ``just local-pro`` workflow, and any test harness
that wants to exercise the Pro code path without pulling in cloud.

Pair with ``DJANGO_SETTINGS_MODULE=config.settings.local_pro``. The
equivalent cloud settings module is ``validibot_cloud.settings.local``
— that one layers cloud apps on top and already includes
``validibot_pro`` in its own common-cloud app list.
"""

from .local import *  # noqa: F403
from .local import INSTALLED_APPS as _BASE_INSTALLED_APPS

INSTALLED_APPS = [*_BASE_INSTALLED_APPS]
if "validibot_pro" not in INSTALLED_APPS:
    # Append rather than insert so Pro-registered URLs and feature
    # registrations run after community bootstraps. The community
    # codebase never imports validibot_pro directly — it reads the
    # registered License via validibot.core.license.get_license() — so
    # ordering here only matters for URL resolution (first match wins
    # but Pro routes live under /app/pro/*, so they don't collide).
    INSTALLED_APPS.append("validibot_pro")

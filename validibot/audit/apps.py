"""Django app configuration for the Validibot audit log."""

from django.apps import AppConfig


class AuditConfig(AppConfig):
    """Register the audit-log app with Django.

    ``ready()`` connects the Session-2 capture hooks (allauth + Django
    auth signals, DRF ``Token`` post-save/post-delete). The middleware
    that populates the per-request context is registered separately in
    ``settings.MIDDLEWARE`` — Django does not provide a hook for an
    app to inject middleware into the settings list.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.audit"
    verbose_name = "Validibot Audit Log"

    def ready(self) -> None:
        """Attach signal receivers that translate auth + model events
        into audit entries.

        Three layers, split across modules to keep each receiver file
        focused on one kind of event:

        * ``signals.connect_signal_receivers`` — allauth / Django auth
          signals + DRF Token create/delete (Session 2).
        * ``model_audit.connect_model_audit_receivers`` + the builtin
          registry — generic pre/post-save + pre-delete dispatch
          driven by ``AUDITABLE_FIELDS`` (Session 3).
        * ``admin_bridge.connect_admin_bridge`` — mirror
          ``admin.LogEntry`` rows into ``AuditLogEntry`` so staff
          actions show up alongside everything else (Session 3).
        """

        from validibot.audit.admin_bridge import connect_admin_bridge
        from validibot.audit.model_audit import connect_model_audit_receivers
        from validibot.audit.model_audit import register_builtin_model_audits
        from validibot.audit.signals import connect_signal_receivers

        connect_signal_receivers()
        connect_model_audit_receivers()
        register_builtin_model_audits()
        connect_admin_bridge()

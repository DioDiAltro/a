from django.apps import AppConfig


class ComplianceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "compliance"

    def ready(self) -> None:
        # Subscribing here rather than at import time means the audit journal is
        # wired up exactly once, after the app registry is populated.
        from compliance import handlers

        handlers.register()

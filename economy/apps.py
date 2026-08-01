from django.apps import AppConfig


class EconomyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "economy"

    def ready(self) -> None:
        # Surveillance only works if it was listening before the thing happened,
        # so the subscriptions are registered at app load.
        from economy import handlers

        handlers.register()

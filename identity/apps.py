from django.apps import AppConfig


class IdentityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "identity"

    def ready(self) -> None:
        # XP is granted by subscribing to settled events, so the subscriptions
        # must exist before the first event is published — i.e. at app load,
        # not on first request.
        from identity.levels import register_handlers

        register_handlers()

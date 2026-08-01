from django.apps import AppConfig


class GamesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "games"

    def ready(self) -> None:
        # Imported here rather than at module scope: `handlers` reaches through
        # services into models, and none of that may run before the app registry
        # is populated.
        from games import handlers

        handlers.register()

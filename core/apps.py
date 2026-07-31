from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        # Registers the boot-time guards, including the one that refuses to
        # start a LIVE_MODE deployment with compliance controls missing.
        import config.checks  # noqa: F401

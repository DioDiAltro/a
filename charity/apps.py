from django.apps import AppConfig


class CharityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "charity"

    def ready(self) -> None:
        # The running record of what was raised is fed by events, so the
        # subscription has to exist before the first fee is taken — i.e. at app
        # load, not on first request.
        from charity import handlers

        handlers.register()

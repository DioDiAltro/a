from django.apps import AppConfig


class SalemodesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "salemodes"
    verbose_name = "Modalità di vendita"

    def ready(self) -> None:
        """Populate the registry.

        Importing the package is the only wiring a mode needs — the `@register`
        decorator does the rest. A mode shipped by another app registers itself
        from that app's `ready()` in exactly the same way, which is what makes
        "a new mechanic without touching the order engine" true rather than
        aspirational.
        """
        from salemodes import modes  # noqa: F401

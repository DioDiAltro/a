from django.apps import AppConfig


class MarketplaceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "marketplace"
    verbose_name = "Mercato"

    def ready(self) -> None:
        """Wire the order engine to the sale context.

        Subscription, not import: `salemodes` has never heard of orders. It
        publishes that a sale settled and this context decides what that means
        for goods, escrow and the seller's balance.
        """
        from marketplace import subscribers  # noqa: F401

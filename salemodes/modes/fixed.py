"""Buy it now. The boring one, and the proof that the seam is not lottery-shaped.

Every hook is implemented, none of them do anything clever, and the order engine
cannot tell the difference between this and the lottery.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.money import Amount
from salemodes import holds
from salemodes.base import (
    Award,
    ParticipationResult,
    SaleMode,
    SettlementResult,
    require_int,
)
from salemodes.constants import FeeScope
from salemodes.registry import register


@register
class FixedPriceMode(SaleMode):
    key = "fixed"
    label = "Prezzo fisso"
    description = "Chi paga il prezzo se lo porta a casa, subito."
    template_partial = "salemodes/modes/_fixed.html"

    fee_scope = FeeScope.WINNER
    requires_deadline = False
    allows_repeat_participation = False

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return {"price_minor": require_int(config, "price_minor", minimum=1, label="prezzo")}

    def initial_closes_at(self, config: dict[str, Any], opens_at: datetime) -> datetime | None:
        """A buy-it-now listing does not expire on its own."""
        return None

    def on_participation(self, sale, user, amount: Amount, units: int) -> ParticipationResult:
        price = sale.amount(int(sale.config["price_minor"]))
        if amount != price:
            return ParticipationResult.refused(f"Il prezzo è {price}.")

        participation = holds.record_participation(sale, user, amount, units=1)
        return ParticipationResult(
            accepted=True,
            participation=participation,
            settle_now=True,
            message=f"Acquistato per {price}.",
        )

    def settle(self, sale) -> SettlementResult:
        participation = sale.active_participations().first()
        if participation is None:
            return SettlementResult.abort("Nessun acquirente.")

        buyer_id = participation.user_id
        gross = holds.capture(participation)
        return SettlementResult(awards=(Award(user_id=buyer_id, gross=gross),))

    def progress(self, sale) -> dict[str, Any]:
        return {"price": sale.amount(int(sale.config.get("price_minor", 0)))}

    def summary(self, sale) -> str:
        return f"Prezzo fisso — {sale.amount(int(sale.config.get('price_minor', 0)))}"

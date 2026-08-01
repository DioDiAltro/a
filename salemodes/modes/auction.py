"""English ascending auction, with anti-sniping.

Only the top bid stays reserved: every time it is beaten, the previous bidder's
hold is released immediately. Bidders are therefore never short of money because
of a bid they have already lost, which is what lets someone follow three
auctions at once with one balance.

On sniping
----------
A bid in the last seconds does not out-value anyone, it out-*clocks* them. It
converts an auction into a reflex test and suppresses the final price, which
hurts the seller as much as the underbidder. So a bid inside the closing window
pushes the close out by the same window: to win you have to be willing to be
beaten again. Extensions are capped so two bots cannot keep a lot open forever
by trading one-scaglia increments.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from core.money import Amount
from salemodes import holds
from salemodes.base import (
    Award,
    ParticipationResult,
    SaleMode,
    SettlementResult,
    require_int,
)
from salemodes.constants import (
    ANTI_SNIPE_EXTENSION_SECONDS,
    ANTI_SNIPE_WINDOW_SECONDS,
    MAX_ANTI_SNIPE_EXTENSIONS,
    FeeScope,
)
from salemodes.registry import register


@register
class AuctionMode(SaleMode):
    key = "auction"
    label = "Asta"
    description = (
        "Rilancio libero fino alla scadenza. Un'offerta negli ultimi due minuti "
        "sposta in avanti la chiusura: niente colpi last-second."
    )
    template_partial = "salemodes/modes/_auction.html"

    fee_scope = FeeScope.WINNER
    requires_deadline = True
    allows_repeat_participation = True

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        start = require_int(config, "start_price_minor", minimum=1, label="base d'asta")
        return {
            "start_price_minor": start,
            "min_increment_minor": require_int(
                config, "min_increment_minor", default=max(1, start // 100), minimum=1,
                label="rilancio minimo",
            ),
            "duration_days": require_int(
                config, "duration_days", default=3, minimum=1, label="durata in giorni"
            ),
        }

    # -- bidding ------------------------------------------------------------

    def top_bid(self, sale):
        return sale.active_participations().order_by("-amount_minor", "-created_at").first()

    def minimum_bid(self, sale) -> Amount:
        top = self.top_bid(sale)
        if top is None:
            return sale.amount(int(sale.config["start_price_minor"]))
        return sale.amount(top.amount_minor + int(sale.config["min_increment_minor"]))

    def on_participation(self, sale, user, amount: Amount, units: int) -> ParticipationResult:
        minimum = self.minimum_bid(sale)
        if amount < minimum:
            return ParticipationResult.refused(f"L'offerta minima ora è {minimum}.")

        previous = self.top_bid(sale)
        participation = holds.record_participation(sale, user, amount, units=1)

        if previous is not None:
            # Released only after the new bid is safely reserved: if the new
            # bidder cannot cover it, the standing bid must survive untouched.
            holds.release(previous, reason="outbid")

        extended_to = self._extend_if_sniped(sale)

        return ParticipationResult(
            accepted=True,
            participation=participation,
            extended_to=extended_to,
            message=(
                f"Offerta di {amount} accettata."
                + (" Chiusura posticipata." if extended_to else "")
            ),
        )

    def _extend_if_sniped(self, sale):
        if sale.closes_at is None:
            return None

        now = timezone.now()
        remaining = (sale.closes_at - now).total_seconds()
        if remaining > ANTI_SNIPE_WINDOW_SECONDS:
            return None

        extensions = int(sale.metadata.get("snipe_extensions", 0))
        if extensions >= MAX_ANTI_SNIPE_EXTENSIONS:
            return None

        sale.closes_at = now + timedelta(seconds=ANTI_SNIPE_EXTENSION_SECONDS)
        sale.metadata = {**sale.metadata, "snipe_extensions": extensions + 1}
        sale.save(update_fields=["closes_at", "metadata", "updated_at"])
        return sale.closes_at

    # -- settlement ---------------------------------------------------------

    def settle(self, sale) -> SettlementResult:
        top = self.top_bid(sale)
        if top is None:
            return SettlementResult.abort("Nessuna offerta.")

        winner_id = top.user_id
        gross = holds.capture(top)
        # Anything still active is a stale losing bid; give it back.
        holds.release_all(sale, reason="auction_closed")

        return SettlementResult(
            awards=(Award(user_id=winner_id, gross=gross, payload={"winning_bid": gross.minor}),)
        )

    def progress(self, sale) -> dict[str, Any]:
        top = self.top_bid(sale)
        return {
            "start_price": sale.amount(int(sale.config.get("start_price_minor", 0))),
            "top_bid": top.amount if top else None,
            "minimum_bid": self.minimum_bid(sale),
            "bid_count": sale.participations.count(),
            "extensions": int(sale.metadata.get("snipe_extensions", 0)),
        }

    def summary(self, sale) -> str:
        top = self.top_bid(sale)
        return f"Asta — offerta più alta {top.amount}" if top else "Asta — nessuna offerta"

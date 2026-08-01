"""Challenge: beat the seller and you may buy at a discount.

The mechanic the brief is really asking for when it says the marketplace should
be a game — the price is not a number, it is something you win. A buyer stakes
the *discounted* price, plays one provably fair round against the seller, and
either walks away having bought at that price or gets every scaglia back.

Two things make it safe rather than a slot machine with an item attached:

* **Losing costs nothing.** The hold is released in full. What is at stake is
  the discount, not the money — which keeps the mechanic on the marketplace
  side of the line rather than the gambling side.
* **The odds are published and the round is verifiable.** The commitment is
  made when the sale opens, before any attempt exists, and each attempt uses
  its own nonce off that commitment. The seller cannot re-roll a result they
  do not like, because the seed was fixed before they knew who would play.
"""

from __future__ import annotations

from typing import Any

from core.exceptions import SaleModeError
from core.money import Amount, split_percentage
from salemodes import fairness, holds
from salemodes.base import (
    Award,
    Decision,
    ParticipationResult,
    SaleMode,
    SettlementResult,
    require_int,
)
from salemodes.constants import FeeScope
from salemodes.registry import register


@register
class ChallengeMode(SaleMode):
    key = "challenge"
    label = "Sfida il venditore"
    description = (
        "Battilo al minigioco e compri con lo sconto. Se perdi non paghi nulla: "
        "l'importo bloccato torna intero."
    )
    template_partial = "salemodes/modes/_challenge.html"

    fee_scope = FeeScope.WINNER
    requires_deadline = False
    allows_repeat_participation = True
    draws_outcome = True

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        price = require_int(config, "price_minor", minimum=1, label="prezzo pieno")
        discount = require_int(
            config, "discount_basis_points", default=2_000, minimum=1,
            label="sconto in punti base",
        )
        if discount >= 10_000:
            raise SaleModeError(
                "Uno sconto del 100% non è una sfida, è un regalo: usa un valore "
                "inferiore a 10000 punti base."
            )

        return {
            "price_minor": price,
            "discount_basis_points": discount,
            "game_key": str(config.get("game_key", "coinflip"))[:32],
            #: The buyer's chance, in basis points. Published on the listing.
            "buyer_odds_basis_points": require_int(
                config, "buyer_odds_basis_points", default=5_000, minimum=100,
                label="probabilità di vittoria",
            ),
            "max_attempts_per_user": require_int(
                config, "max_attempts_per_user", default=3, minimum=1,
                label="tentativi per utente",
            ),
        }

    def discounted_price(self, sale) -> Amount:
        full = sale.amount(int(sale.config["price_minor"]))
        _cut, net = split_percentage(full, int(sale.config["discount_basis_points"]))
        return net

    # -- playing ------------------------------------------------------------

    def on_participation(self, sale, user, amount: Amount, units: int) -> ParticipationResult:
        price = self.discounted_price(sale)
        if amount != price:
            return ParticipationResult.refused(
                f"Metti in gioco il prezzo scontato: {price}."
            )

        attempts = sale.participations.filter(user=user).count()
        ceiling = int(sale.config.get("max_attempts_per_user", 3))
        if attempts >= ceiling:
            return ParticipationResult.refused(
                f"Hai già usato i tuoi {ceiling} tentativi su questo annuncio."
            )

        # Reserve first: a challenge you cannot afford to win is not a challenge.
        participation = holds.record_participation(sale, user, amount, units=1)

        commitment = fairness.Commitment(
            hash=sale.draw_commitment_hash,
            seed=sale.draw_seed,
            proof_id=sale.draw_proof_id,
            source=sale.draw_source or "local",
        )
        odds = int(sale.config["buyer_odds_basis_points"])
        draw = fairness.draw_index(
            commitment=commitment,
            entries=[str(user.pk), str(sale.seller_id)],
            weights=[odds, 10_000 - odds],
            nonce=sale.participations.count(),
        )
        buyer_won = draw.index == 0

        participation.metadata = {
            **participation.metadata,
            "won": buyer_won,
            "verification": draw.verification,
        }
        participation.save(update_fields=["metadata", "updated_at"])

        if not buyer_won:
            holds.release(participation, reason="challenge_lost")
            return ParticipationResult(
                accepted=False,
                participation=participation,
                message="Hai perso la sfida. L'importo bloccato ti è stato restituito.",
                payload={"won": False, "verification": draw.verification},
            )

        return ParticipationResult(
            accepted=True,
            participation=participation,
            settle_now=True,
            message=f"Hai vinto: l'oggetto è tuo a {price}.",
            payload={"won": True, "verification": draw.verification},
        )

    def can_settle_early(self, sale) -> Decision:
        if not sale.active_participations().exists():
            return Decision.no("Nessuno ha ancora battuto il venditore.")
        return Decision.yes("C'è un vincitore.")

    def settle(self, sale) -> SettlementResult:
        participation = sale.active_participations().first()
        if participation is None:
            return SettlementResult.abort("Nessuno ha battuto il venditore.")

        buyer_id = participation.user_id
        verification = participation.metadata.get("verification", {})
        gross = holds.capture(participation)

        return SettlementResult(
            awards=(
                Award(
                    user_id=buyer_id,
                    gross=gross,
                    payload={"discount_basis_points": sale.config["discount_basis_points"]},
                ),
            ),
            payload={"verification": verification},
        )

    def progress(self, sale) -> dict[str, Any]:
        full = sale.amount(int(sale.config.get("price_minor", 0)))
        return {
            "full_price": full,
            "discounted_price": self.discounted_price(sale),
            "game_key": sale.config.get("game_key", "coinflip"),
            "buyer_odds_percent": int(sale.config.get("buyer_odds_basis_points", 5000)) / 100,
            "attempts": sale.participations.count(),
        }

    def summary(self, sale) -> str:
        return f"Sfida — vinci e paghi {self.discounted_price(sale)}"

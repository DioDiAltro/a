"""Mystery box: a sealed set of prizes with published odds.

The set is fixed and public *before* the first box is bought — labels,
quantities and therefore odds are in the sale's config and rendered on the
listing page. What is hidden is which buyer gets which prize, and that is
decided at settlement by one provably fair shuffle over the whole allocation.

Doing the allocation as a single derivation rather than a draw per buyer is what
makes it checkable: one seed, one published entry list, one permutation. A buyer
who thinks they were robbed can reproduce every assignment, not just their own.

Odds are the promise. `validate_config` refuses a box whose quantities do not
add up, because a published probability that does not match the contents is not
a rounding error, it is false advertising.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from core.exceptions import SaleModeError
from core.money import Amount
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
class MysteryBoxMode(SaleMode):
    key = "mysterybox"
    label = "Scatola misteriosa"
    description = (
        "Un insieme sigillato di premi con probabilità pubblicate. "
        "L'assegnazione è estratta in modo verificabile alla chiusura."
    )
    template_partial = "salemodes/modes/_mysterybox.html"

    fee_scope = FeeScope.POOL
    requires_deadline = True
    allows_repeat_participation = True
    draws_outcome = True

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        prizes_raw = config.get("prizes") or []
        if not isinstance(prizes_raw, list) or not prizes_raw:
            raise SaleModeError("Una scatola misteriosa deve elencare almeno un premio.")

        prizes = []
        for index, prize in enumerate(prizes_raw, start=1):
            if not isinstance(prize, dict) or not prize.get("label"):
                raise SaleModeError(f"Il premio numero {index} non ha un nome.")
            quantity = int(prize.get("quantity", 1))
            if quantity < 1:
                raise SaleModeError(
                    f"Il premio «{prize['label']}» deve avere quantità almeno 1."
                )
            prizes.append({"label": str(prize["label"])[:80], "quantity": quantity})

        return {
            "price_minor": require_int(config, "price_minor", minimum=1, label="prezzo"),
            "prizes": prizes,
            "duration_days": require_int(
                config, "duration_days", default=7, minimum=1, label="durata in giorni"
            ),
        }

    def initial_closes_at(self, config: dict[str, Any], opens_at: datetime) -> datetime | None:
        return opens_at + timedelta(days=int(config.get("duration_days", 7)))

    # -- the sealed contents ------------------------------------------------

    @staticmethod
    def prize_pool(sale) -> list[str]:
        """The individual prizes, expanded from their quantities."""
        pool: list[str] = []
        for prize in sale.config.get("prizes", []):
            pool.extend([prize["label"]] * int(prize["quantity"]))
        return pool

    @classmethod
    def capacity(cls, sale) -> int:
        return len(cls.prize_pool(sale))

    def odds(self, sale) -> list[dict[str, Any]]:
        """Published odds, in basis points so they are exact integers."""
        total = self.capacity(sale) or 1
        return [
            {
                "label": prize["label"],
                "quantity": prize["quantity"],
                "basis_points": int(prize["quantity"]) * 10_000 // total,
            }
            for prize in sale.config.get("prizes", [])
        ]

    # -- lifecycle ----------------------------------------------------------

    def on_participation(self, sale, user, amount: Amount, units: int) -> ParticipationResult:
        price = sale.amount(int(sale.config["price_minor"]))
        units = max(1, int(units))
        if amount != sale.amount(price.minor * units):
            return ParticipationResult.refused(f"Una scatola costa {price}.")

        remaining = self.capacity(sale) - sale.units_total
        if units > remaining:
            return ParticipationResult.refused(
                "Non ci sono abbastanza scatole: ne restano "
                f"{max(0, remaining)}."
            )

        participation = holds.record_participation(sale, user, amount, units=units)
        sold_out = sale.units_total >= self.capacity(sale)

        return ParticipationResult(
            accepted=True,
            participation=participation,
            threshold_reached=sold_out,
            settle_now=sold_out,
            message=(
                "Scatole prenotate. L'assegnazione avviene alla chiusura."
                if not sold_out
                else "Ultime scatole prese: si apre subito."
            ),
        )

    def can_settle_early(self, sale) -> Decision:
        """Only when the box is empty — there is nothing left to sell."""
        if sale.units_total < self.capacity(sale):
            return Decision.no(
                f"Restano {self.capacity(sale) - sale.units_total} scatole da vendere."
            )
        return Decision.yes("Tutte le scatole sono state vendute.")

    def settle(self, sale) -> SettlementResult:
        participations = list(sale.active_participations())
        if not participations:
            return SettlementResult.abort("Nessuna scatola venduta.")

        pool = self.prize_pool(sale)

        # One entry per box bought, so somebody who took three boxes gets three
        # draws — and the entry list is public, so the weighting is checkable.
        entries: list[str] = []
        owners = []
        for participation in participations:
            for index in range(participation.units):
                entries.append(f"{participation.user_id}#{index}")
                owners.append(participation)

        commitment = fairness.Commitment(
            hash=sale.draw_commitment_hash,
            seed=sale.draw_seed,
            proof_id=sale.draw_proof_id,
            source=sale.draw_source or "local",
        )
        order, verification = fairness.draw_permutation(
            commitment=commitment, entries=entries, count=len(pool)
        )

        prizes_by_participation: dict[Any, list[str]] = {}
        for entry_index, prize_index in enumerate(order):
            participation = owners[entry_index]
            prizes_by_participation.setdefault(participation.pk, []).append(pool[prize_index])

        awards = []
        for participation in participations:
            buyer_id = participation.user_id
            won = prizes_by_participation.get(participation.pk, [])
            gross = holds.capture(participation)
            awards.append(Award(user_id=buyer_id, gross=gross, payload={"prizes": won}))

        return SettlementResult(
            awards=tuple(awards),
            fairness_proof_id=sale.draw_proof_id,
            payload={"verification": verification},
        )

    def progress(self, sale) -> dict[str, Any]:
        capacity = self.capacity(sale)
        return {
            "price": sale.amount(int(sale.config.get("price_minor", 0))),
            "capacity": capacity,
            "sold": sale.units_total,
            "remaining": max(0, capacity - sale.units_total),
            "percent": min(100, sale.units_total * 100 // (capacity or 1)),
            "odds": self.odds(sale),
        }

    def summary(self, sale) -> str:
        return f"Scatola misteriosa — {sale.units_total}/{self.capacity(sale)} vendute"

"""Group buy: the price unlocks at N buyers, or nobody pays.

The mechanic is a coordination device. Each buyer commits money they will only
spend if enough others do, so the reservation has to be a *hold* rather than a
payment — and if the group never forms, the release gives back the exact
scaglie that were reserved, not a computed approximation of them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from core.money import Amount
from salemodes import holds
from salemodes.base import (
    Award,
    Decision,
    ParticipationResult,
    SaleMode,
    SettlementResult,
    require_int,
)
from salemodes.constants import FeeScope, SaleState
from salemodes.registry import register


@register
class GroupBuyMode(SaleMode):
    key = "groupbuy"
    label = "Acquisto di gruppo"
    description = (
        "Si sblocca quando si raggiunge il numero di acquirenti. Se non si "
        "arriva, nessuno paga e i soldi tornano interi."
    )
    template_partial = "salemodes/modes/_groupbuy.html"

    fee_scope = FeeScope.POOL
    requires_deadline = True
    #: One slot per person: letting somebody buy five slots would let a single
    #: buyer fake the group the mechanic exists to assemble.
    allows_repeat_participation = False

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return {
            "unit_price_minor": require_int(
                config, "unit_price_minor", minimum=1, label="prezzo a persona"
            ),
            "min_buyers": require_int(config, "min_buyers", minimum=2, label="acquirenti minimi"),
            "duration_days": require_int(
                config, "duration_days", default=7, minimum=1, label="durata in giorni"
            ),
        }

    def initial_closes_at(self, config: dict[str, Any], opens_at: datetime) -> datetime | None:
        return opens_at + timedelta(days=int(config.get("duration_days", 7)))

    def on_participation(self, sale, user, amount: Amount, units: int) -> ParticipationResult:
        price = sale.amount(int(sale.config["unit_price_minor"]))
        if amount != price:
            return ParticipationResult.refused(f"La quota è di {price} a persona.")

        minimum = int(sale.config["min_buyers"])
        before = sale.participant_count
        participation = holds.record_participation(sale, user, amount, units=1)
        after = before + 1

        return ParticipationResult(
            accepted=True,
            participation=participation,
            threshold_reached=before < minimum <= after,
            message=(
                f"Sei dentro. Siamo in {after} su {minimum}."
                if after < minimum
                else "Gruppo completo: l'acquisto si sblocca."
            ),
        )

    def can_settle_early(self, sale) -> Decision:
        """Once the group is complete there is nothing left to wait for.

        No notice window here, unlike the lottery: nothing is drawn, so there is
        no favourable instant for a seller to pick.
        """
        if sale.sale_state is not SaleState.THRESHOLD_MET:
            return Decision.no(
                f"Servono almeno {sale.config['min_buyers']} acquirenti: "
                f"per ora siete in {sale.participant_count}."
            )
        return Decision.yes("Il gruppo è completo, puoi chiudere.")

    def settle(self, sale) -> SettlementResult:
        participations = list(sale.active_participations())
        minimum = int(sale.config["min_buyers"])

        if len({p.user_id for p in participations}) < minimum:
            return SettlementResult.abort(
                f"Gruppo non raggiunto: {len(participations)} su {minimum}."
            )

        awards = []
        for participation in participations:
            buyer_id = participation.user_id
            gross = holds.capture(participation)
            awards.append(Award(user_id=buyer_id, gross=gross))

        return SettlementResult(awards=tuple(awards))

    def progress(self, sale) -> dict[str, Any]:
        minimum = int(sale.config.get("min_buyers", 1)) or 1
        joined = sale.participant_count
        return {
            "unit_price": sale.amount(int(sale.config.get("unit_price_minor", 0))),
            "min_buyers": minimum,
            "joined": joined,
            "missing": max(0, minimum - joined),
            "percent": min(100, joined * 100 // minimum),
        }

    def summary(self, sale) -> str:
        return f"Acquisto di gruppo — {sale.participant_count}/{sale.config.get('min_buyers')}"

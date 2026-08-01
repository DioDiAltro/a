"""The lottery: example 1 from the brief, and the mode everything else defends.

    "Metto in vendita una cosa a un minimo di 30 biglie. Se in 10 giorni si
    arriva a 30 posso estrarre, e chi vince se la prende. Non c'è un massimo:
    se si arriva a 40, meglio per me."

Read economically, that is a raffle run by a private seller with the platform
holding the money. Every abuse in that sentence is a timing abuse, so this
module is mostly about time:

* **No maximum, but a minimum.** Tickets keep selling past the threshold. The
  threshold only unlocks a *right* — the seller may now draw early — and
  crossing it publishes `SaleThresholdReached` so the UI can say
  "40 di 30 minime".

* **The seller may not buy tickets in their own lottery.** Otherwise the whole
  thing is a machine for converting other people's biglie into a free lottery
  ticket, and the "minimum" is met by the seller against themselves.

* **An early draw must be announced first.** The seller commits publicly, then
  waits out a notice window, and only then may the draw run. Without the
  window, a seller watches the ticket list and draws at the exact moment a
  confederate holds the largest share. With it, that instant is unknowable:
  anyone may keep buying right up to the draw, and the announcement is binding
  — `settle_due_sales` runs the draw whether or not the seller comes back, so
  announcing and cancelling until the numbers look good is not a strategy.

* **Below the minimum at the deadline, everyone gets everything back.** Via
  `release_hold`, which returns the exact minor units that were reserved. Not a
  computed refund: a computed refund can be wrong.

The draw itself goes through commit-reveal (`salemodes.fairness`, which routes
to `games` when it is available). A lottery whose winner cannot be re-derived by
a suspicious participant is not a lottery, it is a promise.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from core.exceptions import SaleModeError
from core.money import Amount
from salemodes import fairness, holds
from salemodes.base import (
    Award,
    Decision,
    ParticipationResult,
    SaleMode,
    SettlementResult,
    require_bool,
    require_int,
)
from salemodes.constants import (
    DEFAULT_EARLY_DRAW_NOTICE_MINUTES,
    MINIMUM_EARLY_DRAW_NOTICE_MINUTES,
    FeeScope,
    SaleState,
)
from salemodes.registry import register


@register
class LotteryMode(SaleMode):
    key = "lottery"
    label = "Lotteria"
    description = (
        "Vendi biglietti fino a una scadenza. Raggiunto il minimo puoi estrarre "
        "in anticipo, annunciandolo in anticipo. Non c'è un massimo: se si "
        "supera il minimo, tanto meglio."
    )
    template_partial = "salemodes/modes/_lottery.html"

    fee_scope = FeeScope.POOL
    requires_deadline = True
    allows_repeat_participation = True

    #: The rule that makes the minimum mean something.
    allows_seller_participation = False

    #: A drawn outcome: the sale commits to a seed before it can know the entries.
    draws_outcome = True

    # -- configuration ------------------------------------------------------

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        ticket_price = require_int(
            config, "ticket_price", minimum=1, label="prezzo del biglietto"
        )
        minimum_minor = require_int(config, "minimum_minor", minimum=1, label="minimo")

        if minimum_minor < ticket_price:
            raise SaleModeError(
                "Il minimo non può essere inferiore al prezzo di un biglietto: "
                "sarebbe già raggiunto alla prima vendita."
            )

        notice = require_int(
            config, "early_draw_notice_minutes",
            default=DEFAULT_EARLY_DRAW_NOTICE_MINUTES,
            minimum=MINIMUM_EARLY_DRAW_NOTICE_MINUTES,
            label="preavviso di estrazione",
        )

        deadline_days = config.get("deadline_days", config.get("deadline"))
        normalised: dict[str, Any] = {
            "ticket_price": ticket_price,
            "minimum_minor": minimum_minor,
            "early_draw_allowed": require_bool(config, "early_draw_allowed", default=True),
            "early_draw_notice_minutes": notice,
            "payout_in_biglie": require_bool(config, "payout_in_biglie", default=True),
            "max_tickets_per_user": require_int(
                config, "max_tickets_per_user", default=0, minimum=0,
                label="biglietti massimi per utente",
            ),
        }

        if isinstance(deadline_days, str) and not deadline_days.isdigit():
            # An explicit ISO deadline. Parsed here so an unparseable one is a
            # listing-time error rather than a task-time surprise at 3am.
            parsed = datetime.fromisoformat(deadline_days)
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed)
            normalised["deadline"] = parsed.isoformat()
        else:
            normalised["deadline_days"] = require_int(
                config, "deadline_days", default=deadline_days or 10, minimum=1,
                label="durata in giorni",
            )

        return normalised

    def initial_closes_at(self, config: dict[str, Any], opens_at: datetime) -> datetime | None:
        if config.get("deadline"):
            parsed = datetime.fromisoformat(config["deadline"])
            return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
        return opens_at + timedelta(days=int(config.get("deadline_days", 10)))

    # -- selling tickets ----------------------------------------------------

    def on_participation(self, sale, user, amount: Amount, units: int) -> ParticipationResult:
        ticket_price = int(sale.config["ticket_price"])
        units = max(1, int(units))
        expected = sale.amount(ticket_price * units)

        if amount != expected:
            return ParticipationResult.refused(
                f"Un biglietto costa {sale.amount(ticket_price)}; "
                f"per {units} servono {expected}."
            )

        ceiling = int(sale.config.get("max_tickets_per_user", 0))
        if ceiling:
            already = sum(
                p.units for p in sale.active_participations().filter(user=user)
            )
            if already + units > ceiling:
                return ParticipationResult.refused(
                    f"Puoi comprare al massimo {ceiling} biglietti in questa lotteria."
                )

        was_below = sale.total_minor < int(sale.config["minimum_minor"])
        participation = holds.record_participation(sale, user, amount, units=units)
        crossed = was_below and sale.total_minor >= int(sale.config["minimum_minor"])

        return ParticipationResult(
            accepted=True,
            participation=participation,
            threshold_reached=crossed,
            message=(
                f"{units} biglietti acquistati. "
                f"Montepremi: {sale.total} su {sale.amount(int(sale.config['minimum_minor']))} minime."
            ),
        )

    # -- drawing ------------------------------------------------------------

    def can_announce_settlement(self, sale) -> Decision:
        """Announcing is allowed once the minimum is in, and not before.

        The threshold is the seller's side of the bargain: below it, the
        participants were promised a deadline, and an early draw would be the
        seller changing the rules after seeing the ticket list.
        """
        if not sale.config.get("early_draw_allowed", True):
            return Decision.no("Questa lotteria non prevede l'estrazione anticipata.")
        if sale.sale_state is not SaleState.THRESHOLD_MET:
            return Decision.no(
                f"Puoi annunciare l'estrazione solo dopo il minimo di "
                f"{sale.amount(int(sale.config['minimum_minor']))}. Ora sei a {sale.total}."
            )
        return Decision.yes("Puoi annunciare l'estrazione.")

    def can_settle_early(self, sale) -> Decision:
        """The three guards, in the order a seller meets them.

        Every one of these is a refusal a seller will be annoyed by, and every
        one of them exists because without it the draw could be steered.
        """
        if not sale.config.get("early_draw_allowed", True):
            return Decision.no("Questa lotteria non prevede l'estrazione anticipata.")

        if sale.sale_state is not SaleState.THRESHOLD_MET:
            return Decision.no(
                f"Puoi estrarre solo dopo aver raggiunto il minimo di "
                f"{sale.amount(int(sale.config['minimum_minor']))}. "
                f"Ora sei a {sale.total}."
            )

        if not sale.is_announced:
            minutes = int(sale.early_draw_notice.total_seconds() // 60)
            return Decision.no(
                f"Devi annunciare l'estrazione con {minutes} minuti di anticipo. "
                "Chi vuole partecipare deve avere il tempo di farlo."
            )

        if not sale.notice_elapsed:
            return Decision.no(
                f"Estrazione annunciata: mancano {sale.seconds_until_draw // 60 + 1} minuti. "
                "Fino ad allora si può ancora partecipare."
            )

        return Decision.yes("Puoi estrarre.")

    def settle(self, sale) -> SettlementResult:
        minimum = int(sale.config["minimum_minor"])
        participations = list(sale.active_participations())

        if not participations:
            return SettlementResult.abort("Nessun biglietto venduto.")

        if sale.total_minor < minimum:
            # The deadline arrived below the minimum. Nobody wins, nobody pays;
            # `services.settle_sale` turns this into a refund and `SaleAborted`.
            return SettlementResult.abort(
                f"Minimo non raggiunto: {sale.total} su {sale.amount(minimum)}."
            )

        entries = [str(p.user_id) for p in participations]
        weights = [p.units for p in participations]

        commitment = fairness.Commitment(
            hash=sale.draw_commitment_hash,
            seed=sale.draw_seed,
            proof_id=sale.draw_proof_id,
            source=sale.draw_source or "local",
        )
        draw = fairness.draw_index(commitment=commitment, entries=entries, weights=weights)
        winner = participations[draw.index]

        gross = holds.capture_all(sale)

        return SettlementResult(
            awards=(
                Award(
                    user_id=winner.user_id,
                    gross=gross,
                    payload={
                        "ticket_units": winner.units,
                        "tickets_sold": sum(weights),
                        "participants": len(entries),
                    },
                ),
            ),
            fairness_proof_id=draw.proof_id,
            payload={"verification": draw.verification},
        )

    # -- presentation -------------------------------------------------------

    def progress(self, sale) -> dict[str, Any]:
        minimum = int(sale.config.get("minimum_minor", 0)) or 1
        return {
            "minimum": sale.amount(minimum),
            "ticket_price": sale.amount(int(sale.config.get("ticket_price", 0))),
            "tickets_sold": sale.units_total,
            "pot": sale.total,
            "percent": min(100, sale.total_minor * 100 // minimum),
            "threshold_met": sale.sale_state is SaleState.THRESHOLD_MET,
            "announced": sale.is_announced,
            "seconds_until_draw": sale.seconds_until_draw,
            "minutes_until_draw": (sale.seconds_until_draw + 59) // 60,
            "early_draw_allowed": bool(sale.config.get("early_draw_allowed", True)),
        }

    def summary(self, sale) -> str:
        minimum = sale.amount(int(sale.config.get("minimum_minor", 0)))
        return f"Lotteria — {sale.total} raccolte su {minimum} minime"

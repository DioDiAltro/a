"""Event subscriptions that keep the surveillance books fed.

`economy` watches the whole platform and imports almost none of it. Everything
in this module is a listener: revenue as it is recognised, mints as they happen,
rounds as they settle. Nothing here writes to the ledger, and nothing here can
fail in a way that undoes what it is recording — by the time a handler runs, the
money has already moved.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError
from django.utils import timezone

from core.contracts import (
    CHARITY_BASIS_POINTS,
    REDISTRIBUTION_BASIS_POINTS,
    CreditClaimed,
    DepositConfirmed,
    GameSettled,
    RevenueRecognised,
    WithdrawalRequested,
)
from core.events import bus
from core.money import Amount, split_percentage
from economy.constants import CLAIM_GAMBLE_KEY
from economy.models import (
    GameRoundOutcome,
    MintRecord,
    RevenueRecord,
    WithdrawalRecord,
)

logger = logging.getLogger("economy")


def _aware(moment):
    """`DomainEvent.occurred_at` is `datetime.now()` — naive. This project is not."""
    if moment is None:
        return timezone.now()
    if timezone.is_naive(moment):
        return timezone.make_aware(moment, timezone.get_default_timezone())
    return moment


def _safely(what: str, fn, *args, **kwargs) -> None:
    """Run a handler body without letting it take down the publisher.

    Handlers run after commit. Losing a surveillance row is a monitoring gap
    worth logging loudly; raising here would only kill a post-commit hook long
    after the transaction it describes is safely on disk.
    """
    try:
        fn(*args, **kwargs)
    except IntegrityError:
        # Unique on event_id: a redelivered event. Expected, not an error.
        logger.info("Ignored redelivered event for %s", what)
    except Exception:
        logger.exception("economy failed to record %s", what)


# ---------------------------------------------------------------------------
# Revenue and supply
# ---------------------------------------------------------------------------


def _record_revenue(event: RevenueRecognised) -> None:
    amount = Amount(event.amount_minor, event.asset)
    charity, _ = split_percentage(amount, CHARITY_BASIS_POINTS)
    redistribution, _ = split_percentage(amount, REDISTRIBUTION_BASIS_POINTS)

    RevenueRecord.objects.get_or_create(
        event_id=event.event_id,
        defaults={
            "amount_minor": event.amount_minor,
            "asset": str(event.asset),
            "source": event.source,
            "reference_id": str(event.reference_id or ""),
            "charity_minor": charity.minor,
            "redistribution_minor": redistribution.minor,
            "occurred_at": _aware(event.occurred_at),
        },
    )


def on_revenue_recognised(event: RevenueRecognised) -> None:
    if event.amount_minor <= 0:
        return
    _safely("RevenueRecognised", _record_revenue, event)


def _record_mint(event: DepositConfirmed) -> None:
    MintRecord.objects.get_or_create(
        event_id=event.event_id,
        defaults={
            "deposit_id": event.deposit_id,
            "user_id": event.user_id,
            "euro_minor": event.euro_minor,
            "biglie_minor": event.biglie_minor,
            "occurred_at": _aware(event.occurred_at),
        },
    )


def on_deposit_confirmed(event: DepositConfirmed) -> None:
    """The only event in the system that creates biglie. Counted, always."""
    _safely("DepositConfirmed", _record_mint, event)


def _record_withdrawal(event: WithdrawalRequested) -> None:
    WithdrawalRecord.objects.get_or_create(
        event_id=event.event_id,
        defaults={
            "withdrawal_id": event.withdrawal_id,
            "user_id": event.user_id,
            "euro_minor": event.euro_minor,
            "occurred_at": _aware(event.occurred_at),
        },
    )


def on_withdrawal_requested(event: WithdrawalRequested) -> None:
    _safely("WithdrawalRequested", _record_withdrawal, event)


# ---------------------------------------------------------------------------
# Rounds, for the RTP watchdog
# ---------------------------------------------------------------------------


def _record_round(event: GameSettled) -> None:
    GameRoundOutcome.objects.get_or_create(
        round_id=event.round_id,
        defaults={
            "game_key": event.game_key,
            "origin": GameRoundOutcome.Origin.MINIGAME,
            "user_id": event.user_id,
            "stake_minor": event.stake_minor,
            "payout_minor": event.payout_minor,
            "asset": str(event.asset),
            "settled_at": _aware(event.occurred_at),
        },
    )


def on_game_settled(event: GameSettled) -> None:
    _safely("GameSettled", _record_round, event)


def _record_claim_gamble(event: CreditClaimed) -> None:
    GameRoundOutcome.objects.get_or_create(
        round_id=event.credit_id,
        defaults={
            "game_key": CLAIM_GAMBLE_KEY,
            "origin": GameRoundOutcome.Origin.CLAIM,
            "user_id": event.user_id,
            "stake_minor": event.original_minor,
            "payout_minor": event.settled_minor,
            "asset": str(event.asset),
            "settled_at": _aware(event.occurred_at),
        },
    )


def on_credit_claimed(event: CreditClaimed) -> None:
    """Only the gambled half.

    A safe claim is a fee, not a wager: it already arrives here as revenue, and
    counting it as a round would drag the measured RTP toward 99% and hide a
    genuinely broken game behind the average.
    """
    if not event.risked:
        return
    _safely("CreditClaimed", _record_claim_gamble, event)


_registered = False


def register() -> None:
    global _registered
    if _registered:
        return
    bus.subscribe(RevenueRecognised, on_revenue_recognised)
    bus.subscribe(DepositConfirmed, on_deposit_confirmed)
    bus.subscribe(WithdrawalRequested, on_withdrawal_requested)
    bus.subscribe(GameSettled, on_game_settled)
    bus.subscribe(CreditClaimed, on_credit_claimed)
    _registered = True


__all__ = [
    "on_credit_claimed",
    "on_deposit_confirmed",
    "on_game_settled",
    "on_revenue_recognised",
    "on_withdrawal_requested",
    "register",
]

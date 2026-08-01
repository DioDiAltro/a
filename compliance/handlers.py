"""Event subscriptions that keep the audit journal fed.

Registered from `CompliancerConfig.ready()`. Every money-moving event in
`core.contracts` lands here and becomes a chained journal row.

Subscribing rather than calling means no other context has to remember to log.
A developer adding a new payout path gets audit coverage by publishing the event
they were going to publish anyway, which is the only kind of compliance control
that survives contact with a deadline.
"""

from __future__ import annotations

import logging

from compliance.models import AuditEvent
from core.contracts import (
    CreditClaimed,
    DepositConfirmed,
    DripAwarded,
    FundsTransferred,
    GameSettled,
    SelfExclusionStarted,
    WithdrawalRequested,
)
from core.events import bus

logger = logging.getLogger("compliance.audit")


def _record(event_name: str, *, user_id=None, amount_minor=None, asset="", **payload) -> None:
    try:
        AuditEvent.record(
            event_name=event_name,
            user_id=user_id,
            amount_minor=amount_minor,
            asset=str(asset or ""),
            payload=payload,
        )
    except Exception:
        # Handlers run post-commit; the money already moved. Losing a journal
        # row is serious and worth paging about, but re-raising here would only
        # crash a background thread without undoing anything.
        logger.exception("Failed to record audit event %s", event_name)


def on_deposit(event: DepositConfirmed) -> None:
    _record(
        "payments.deposit_confirmed",
        user_id=event.user_id,
        amount_minor=event.euro_minor,
        asset="EUR",
        deposit_id=str(event.deposit_id),
        biglie_minor=event.biglie_minor,
    )


def on_withdrawal(event: WithdrawalRequested) -> None:
    _record(
        "payments.withdrawal_requested",
        user_id=event.user_id,
        amount_minor=event.euro_minor,
        asset="EUR",
        withdrawal_id=str(event.withdrawal_id),
    )


def on_transfer(event: FundsTransferred) -> None:
    """Peer transfers are the highest-value rows in the journal.

    User-to-user movement is the shape an investigator looks at first, so both
    ends of the transfer are captured on one row rather than inferred by joining
    two.
    """
    _record(
        "wallet.funds_transferred",
        user_id=event.from_user_id,
        amount_minor=event.amount_minor,
        asset=event.asset,
        to_user_id=str(event.to_user_id) if event.to_user_id else None,
        reason=event.reason,
    )


def on_game_settled(event: GameSettled) -> None:
    _record(
        "games.settled",
        user_id=event.user_id,
        amount_minor=event.stake_minor,
        asset=event.asset,
        round_id=str(event.round_id),
        game_key=event.game_key,
        payout_minor=event.payout_minor,
    )


def on_credit_claimed(event: CreditClaimed) -> None:
    _record(
        "wallet.credit_claimed",
        user_id=event.user_id,
        amount_minor=event.settled_minor,
        asset=event.asset,
        credit_id=str(event.credit_id),
        original_minor=event.original_minor,
        risked=event.risked,
    )


def on_drip(event: DripAwarded) -> None:
    _record(
        "economy.drip_awarded",
        user_id=event.user_id,
        amount_minor=event.amount_minor,
        asset=event.asset,
    )


def on_self_exclusion(event: SelfExclusionStarted) -> None:
    _record(
        "compliance.self_exclusion_started",
        user_id=event.user_id,
        until=event.until.isoformat() if event.until else None,
    )


def register() -> None:
    bus.subscribe(DepositConfirmed, on_deposit)
    bus.subscribe(WithdrawalRequested, on_withdrawal)
    bus.subscribe(FundsTransferred, on_transfer)
    bus.subscribe(GameSettled, on_game_settled)
    bus.subscribe(CreditClaimed, on_credit_claimed)
    bus.subscribe(DripAwarded, on_drip)
    bus.subscribe(SelfExclusionStarted, on_self_exclusion)

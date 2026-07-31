"""Payment state machines and the policy dials for real-money movement.

Everything in this module is a *number a regulator could ask about*, so it lives
in one place with the reasoning attached rather than being scattered across
views. The states are shared by deposits and withdrawals on purpose: both are a
single movement of real euros with the same five possible fates, and giving them
one vocabulary means the audit query is one query.
"""

from __future__ import annotations

import enum


class PaymentState(enum.StrEnum):
    """The life of a real-money movement.

    INITIATED  the user asked; nothing has left or entered any account yet
    PENDING    the provider (or a human reviewer) has it; money is in flight
    SUCCEEDED  settled — biglie minted (deposit) or burned (withdrawal)
    FAILED     terminal, no value moved (or every leg was undone)
    REVERSED   it settled and was *then* undone: chargeback, recalled payout
    """

    INITIATED = "initiated"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REVERSED = "reversed"

    @property
    def label(self) -> str:
        return {
            PaymentState.INITIATED: "Avviato",
            PaymentState.PENDING: "In corso",
            PaymentState.SUCCEEDED: "Completato",
            PaymentState.FAILED: "Non riuscito",
            PaymentState.REVERSED: "Stornato",
        }[self]

    @property
    def is_settled(self) -> bool:
        return self is PaymentState.SUCCEEDED

    @property
    def is_terminal(self) -> bool:
        return self in (PaymentState.FAILED, PaymentState.REVERSED)


#: A deposit may only ever walk this graph. REVERSED is reachable only from
#: SUCCEEDED because you cannot charge back money that never settled.
DEPOSIT_TRANSITIONS: dict[PaymentState, frozenset[PaymentState]] = {
    PaymentState.INITIATED: frozenset({PaymentState.PENDING, PaymentState.FAILED}),
    PaymentState.PENDING: frozenset({PaymentState.SUCCEEDED, PaymentState.FAILED}),
    PaymentState.SUCCEEDED: frozenset({PaymentState.REVERSED}),
    PaymentState.FAILED: frozenset(),
    PaymentState.REVERSED: frozenset(),
}

#: Withdrawals share the shape. INITIATED is the unconfirmed draft the user is
#: still looking at; PENDING means the funds are held and either a reviewer or
#: the payout provider owns the next move.
WITHDRAWAL_TRANSITIONS: dict[PaymentState, frozenset[PaymentState]] = {
    PaymentState.INITIATED: frozenset({PaymentState.PENDING, PaymentState.FAILED}),
    PaymentState.PENDING: frozenset({PaymentState.SUCCEEDED, PaymentState.FAILED}),
    PaymentState.SUCCEEDED: frozenset({PaymentState.REVERSED}),
    PaymentState.FAILED: frozenset(),
    PaymentState.REVERSED: frozenset(),
}


class ProviderKey(enum.StrEnum):
    STRIPE = "stripe"
    #: No network, deterministic references, real HMAC signatures. Used by tests
    #: and by any deployment with LIVE_MODE off.
    FAKE = "fake"


# ---------------------------------------------------------------------------
# Policy dials
# ---------------------------------------------------------------------------

#: Below this a card fee eats the whole deposit; above it we are a bank.
MIN_DEPOSIT_MINOR = 500  # €5.00
MAX_DEPOSIT_MINOR = 500_000  # €5,000.00 per transaction

MIN_WITHDRAWAL_MINOR = 1_000  # 10 biglie — below this the payout costs more
MAX_WITHDRAWAL_MINOR = 500_000

#: A verified user withdrawing less than this is paid without a human looking.
#: Everything above queues for manual review; the money stays held meanwhile, so
#: waiting never means the user can re-spend what they asked to take out.
AUTO_APPROVE_MAX_MINOR = 20_000  # €200.00

#: Deliberately absent: any notion of a "cooling-off" window during which a
#: requested withdrawal can be cancelled to put the money back in play. See the
#: comment in `payments.services.approve_withdrawal`.

#: How the withdrawal hold is labelled in the wallet.
WITHDRAWAL_HOLD_PURPOSE = "withdrawal"

#: Stripe event types we act on. Anything else is acknowledged and ignored —
#: returning a non-2xx for an event we simply do not care about would make
#: Stripe retry it forever.
HANDLED_EVENT_TYPES = frozenset(
    {
        "payment_intent.succeeded",
        "payment_intent.payment_failed",
        "payment_intent.canceled",
        "charge.refunded",
        "charge.dispute.created",
        "payout.paid",
        "payout.failed",
    }
)

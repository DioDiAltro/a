"""The cross-context contract: every event that crosses a bounded context.

This file is the coordination point of the whole codebase. A context may import
events from here and publish or subscribe to them. It may **not** import another
context's models, services, forms or tasks. `tests/test_boundaries.py` enforces
that mechanically.

If you need something from another context that is not expressed here, the
answer is a new event or a new entry in that context's `api.py` — never an
import that reaches across.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import ClassVar
from uuid import UUID

from core.events import DomainEvent
from core.money import Asset

# =============================================================================
# wallet
# =============================================================================


@dataclass(frozen=True, slots=True)
class CreditPending(DomainEvent):
    """Value arrived for a user but is NOT yet spendable.

    Every inbound biglia in the system passes through this state — winnings,
    gifts, refunds, drips, sale proceeds. Nothing credits an available balance
    directly. That single chokepoint is what makes the claim ritual (safe vs
    risky) universal instead of bolted onto one feature.
    """

    name: ClassVar[str] = "wallet.credit_pending"

    credit_id: UUID
    user_id: UUID
    amount_minor: int
    asset: Asset
    source: str  # "lottery_prize" | "gift" | "drip" | "sale_proceeds" | "duel_win" | ...
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CreditClaimed(DomainEvent):
    """A pending credit was resolved into the available balance."""

    name: ClassVar[str] = "wallet.credit_claimed"

    credit_id: UUID
    user_id: UUID
    #: What the user actually walked away with — may be zero if they gambled and
    #: lost, or a multiple of the original if they gambled and won.
    settled_minor: int
    original_minor: int
    asset: Asset
    risked: bool


@dataclass(frozen=True, slots=True)
class FundsTransferred(DomainEvent):
    name: ClassVar[str] = "wallet.funds_transferred"

    from_user_id: UUID | None  # None when the platform is the source
    to_user_id: UUID | None
    amount_minor: int
    asset: Asset
    reason: str


@dataclass(frozen=True, slots=True)
class HoldPlaced(DomainEvent):
    """Funds reserved against a listing, ticket or wager but not yet spent."""

    name: ClassVar[str] = "wallet.hold_placed"

    hold_id: UUID
    user_id: UUID
    amount_minor: int
    asset: Asset
    purpose: str


# =============================================================================
# marketplace / salemodes
# =============================================================================


@dataclass(frozen=True, slots=True)
class SaleOpened(DomainEvent):
    name: ClassVar[str] = "sale.opened"

    sale_id: UUID
    listing_id: UUID
    seller_id: UUID
    mode: str  # registry key: "fixed" | "auction" | "lottery" | "groupbuy" | ...
    closes_at: datetime | None


@dataclass(frozen=True, slots=True)
class SaleParticipation(DomainEvent):
    """Someone bought a ticket, placed a bid, joined a group buy."""

    name: ClassVar[str] = "sale.participation"

    sale_id: UUID
    user_id: UUID
    amount_minor: int
    asset: Asset
    units: int


@dataclass(frozen=True, slots=True)
class SaleThresholdReached(DomainEvent):
    """A lottery hit its minimum. From here the seller may draw early.

    Carries the current total so the UI can show "40 di 30 minime — puoi
    estrarre ora" without querying back into the sale context.
    """

    name: ClassVar[str] = "sale.threshold_reached"

    sale_id: UUID
    seller_id: UUID
    total_minor: int
    threshold_minor: int
    asset: Asset


@dataclass(frozen=True, slots=True)
class SaleSettled(DomainEvent):
    """The sale produced a winner/buyer and value must now move."""

    name: ClassVar[str] = "sale.settled"

    sale_id: UUID
    listing_id: UUID
    seller_id: UUID
    winner_id: UUID | None
    gross_minor: int
    asset: Asset
    #: The seller's choice, made at listing time: take euros or take biglie.
    payout_in_biglie: bool
    #: Present for lotteries and mystery boxes; lets anyone re-run the draw.
    fairness_proof_id: UUID | None


@dataclass(frozen=True, slots=True)
class SaleAborted(DomainEvent):
    """Deadline passed below threshold, or the seller cancelled. Refund everyone."""

    name: ClassVar[str] = "sale.aborted"

    sale_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class OrderFulfilled(DomainEvent):
    name: ClassVar[str] = "marketplace.order_fulfilled"

    order_id: UUID
    buyer_id: UUID
    seller_id: UUID


# =============================================================================
# games
# =============================================================================


@dataclass(frozen=True, slots=True)
class GameSettled(DomainEvent):
    """A minigame round finished.

    `stake_minor` and `payout_minor` are what the economy context integrates to
    compute realised RTP; if realised drifts from configured, something is wrong
    with an outcome table and the alert fires.
    """

    name: ClassVar[str] = "games.settled"

    round_id: UUID
    user_id: UUID
    game_key: str
    stake_minor: int
    payout_minor: int
    asset: Asset
    fairness_proof_id: UUID


@dataclass(frozen=True, slots=True)
class DuelChallenged(DomainEvent):
    """One seller staked biglie to challenge another. Needs consent to proceed."""

    name: ClassVar[str] = "games.duel_challenged"

    duel_id: UUID
    challenger_id: UUID
    defender_id: UUID
    stake_minor: int
    asset: Asset
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DuelResolved(DomainEvent):
    name: ClassVar[str] = "games.duel_resolved"

    duel_id: UUID
    winner_id: UUID
    loser_id: UUID
    pot_minor: int
    asset: Asset
    fairness_proof_id: UUID


# =============================================================================
# payments / economy / charity
# =============================================================================


@dataclass(frozen=True, slots=True)
class DepositConfirmed(DomainEvent):
    """Real euros landed. This is the only event that mints BIGLIA_PIENA."""

    name: ClassVar[str] = "payments.deposit_confirmed"

    deposit_id: UUID
    user_id: UUID
    euro_minor: int
    biglie_minor: int


@dataclass(frozen=True, slots=True)
class WithdrawalRequested(DomainEvent):
    name: ClassVar[str] = "payments.withdrawal_requested"

    withdrawal_id: UUID
    user_id: UUID
    euro_minor: int


@dataclass(frozen=True, slots=True)
class RevenueRecognised(DomainEvent):
    """The house took a cut — marketplace fee, game edge, claim fee.

    Both the charity split and the redistribution pool feed off this event, and
    only off this event. That is what guarantees the passive drip can never pay
    out more than the platform actually earned.
    """

    name: ClassVar[str] = "economy.revenue_recognised"

    amount_minor: int
    asset: Asset
    source: str
    reference_id: UUID | None


@dataclass(frozen=True, slots=True)
class CharityAllocated(DomainEvent):
    name: ClassVar[str] = "charity.allocated"

    amount_minor: int
    asset: Asset
    basis_points: int
    beneficiary_id: UUID | None


@dataclass(frozen=True, slots=True)
class DripAwarded(DomainEvent):
    """The idle-user drip paid out of the redistribution pool."""

    name: ClassVar[str] = "economy.drip_awarded"

    user_id: UUID
    amount_minor: int
    asset: Asset


# =============================================================================
# identity / compliance
# =============================================================================


@dataclass(frozen=True, slots=True)
class UserRegistered(DomainEvent):
    name: ClassVar[str] = "identity.user_registered"

    user_id: UUID


@dataclass(frozen=True, slots=True)
class KycStatusChanged(DomainEvent):
    name: ClassVar[str] = "identity.kyc_status_changed"

    user_id: UUID
    tier: str
    verified: bool


@dataclass(frozen=True, slots=True)
class SelfExclusionStarted(DomainEvent):
    """The user pulled the cord. Every context must stop soliciting them.

    Handlers are expected to cancel open sales, refund staked wagers, and
    suppress notifications — not merely block login.
    """

    name: ClassVar[str] = "compliance.self_exclusion_started"

    user_id: UUID
    until: datetime | None  # None = permanent


@dataclass(frozen=True, slots=True)
class LimitBreached(DomainEvent):
    name: ClassVar[str] = "compliance.limit_breached"

    user_id: UUID
    limit_name: str
    attempted_minor: int


# =============================================================================
# Economic constants
# =============================================================================
# Tuned in one place so the economy can be reasoned about as a whole. Every
# figure here is a *sink* except the exchange rate; see docs/ECONOMY.md for why
# the sinks must out-weigh the sources.

#: 1 euro buys 1 biglia. Kept at parity deliberately: a user must never have to
#: do mental arithmetic to know what they are risking.
EURO_PER_BIGLIA = Decimal("1.00")

#: Share of every unit of platform revenue that goes to charity. Applied to the
#: house's cut, not to gross turnover — 3% of turnover would exceed the margin
#: on most transactions and the platform would pay to operate.
CHARITY_BASIS_POINTS = 300  # 3%

#: Marketplace commission on a completed sale.
MARKETPLACE_FEE_BASIS_POINTS = 500  # 5%

#: Taken when a user claims a pending credit *without* gambling. Deliberately
#: small: it nudges toward the risky claim without punishing caution.
SAFE_CLAIM_FEE_BASIS_POINTS = 100  # 1%

#: The house edge on the claim gamble and the minigames. 6% edge => 94% RTP.
DEFAULT_GAME_RTP_BASIS_POINTS = 9_400

#: Rake on a seller-vs-seller duel pot.
DUEL_RAKE_BASIS_POINTS = 400  # 4%

#: Share of recognised revenue diverted into the pool that funds the passive
#: drip. The drip is redistribution, never issuance.
REDISTRIBUTION_BASIS_POINTS = 1_000  # 10% of revenue

#: Unclaimed pending credits expire after this long and fall into the
#: redistribution pool, where they fund other users' drips.
PENDING_CREDIT_TTL_DAYS = 30

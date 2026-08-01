"""Marketplace vocabulary and the commission policy.

The fee rates are here rather than in `core.contracts` because they are a
*marketplace* decision — the platform-wide reference rate lives in core and
these two straddle it.
"""

from __future__ import annotations

import enum
from datetime import timedelta

from core.contracts import MARKETPLACE_FEE_BASIS_POINTS

# ---------------------------------------------------------------------------
# Commission
# ---------------------------------------------------------------------------
# The seller picks how they want to be paid at listing time, and the two rates
# straddle the platform reference of MARKETPLACE_FEE_BASIS_POINTS (5%). This is
# the one lever in the whole system that changes a user's behaviour rather than
# their balance, so it is worth stating plainly what it is buying:
#
# A seller paid in biglie leaves the value inside the economy. Those biglie get
# spent on other listings, staked in other games, and earn the platform another
# commission on the way — so a lower rate is not generosity, it is the value of
# the second transaction being priced in.
#
# A seller paid in euros is an exit. The value leaves, we carry the processing
# and redemption cost, and nothing follows it. Charging more for that is the
# only thing that stops the marketplace becoming a euro-to-euro pipe with a
# game bolted on. Deliberately not punitive: 7% is a normal marketplace rate,
# and a seller who wants out must always be able to get out.

#: Seller takes biglie. Below the platform reference.
BIGLIE_PAYOUT_FEE_BASIS_POINTS = 350  # 3.5%

#: Seller wants to be able to cash out to euros. Above the reference.
EURO_PAYOUT_FEE_BASIS_POINTS = 700  # 7%

#: Sanity: the reference rate must sit between the two, or the blend documented
#: in docs/ECONOMY.md stops being true.
assert BIGLIE_PAYOUT_FEE_BASIS_POINTS < MARKETPLACE_FEE_BASIS_POINTS
assert EURO_PAYOUT_FEE_BASIS_POINTS > MARKETPLACE_FEE_BASIS_POINTS


def fee_basis_points(*, payout_in_biglie: bool) -> int:
    """The commission this seller pays, given how they chose to be paid."""
    return BIGLIE_PAYOUT_FEE_BASIS_POINTS if payout_in_biglie else EURO_PAYOUT_FEE_BASIS_POINTS


# ---------------------------------------------------------------------------
# Goods
# ---------------------------------------------------------------------------


class ListingKind(enum.StrEnum):
    """What is actually being sold. Drives the whole settlement path.

    Three kinds because they differ in *when the buyer is safe*: a digital key
    is safe the instant it is handed over, a parcel is safe when it arrives, and
    a wager is safe only once the world has decided. Escrow policy follows from
    that and from nothing else.
    """

    PHYSICAL = "physical"
    DIGITAL = "digital"
    #: A user-created wager proposition. Settles when its resolution source says
    #: so, which is always after the sale itself has closed.
    BET = "bet"

    @property
    def label(self) -> str:
        return {
            ListingKind.PHYSICAL: "Oggetto fisico",
            ListingKind.DIGITAL: "Bene digitale",
            ListingKind.BET: "Scommessa",
        }[self]

    @property
    def needs_escrow(self) -> bool:
        """Whether the seller waits for the money.

        Digital goods do not: delivery is instantaneous and provable, so
        holding the seller's money for two weeks would be theatre.
        """
        return self is not ListingKind.DIGITAL


class ListingState(enum.StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SOLD = "sold"
    CLOSED = "closed"

    @property
    def label(self) -> str:
        return {
            ListingState.DRAFT: "Bozza",
            ListingState.ACTIVE: "In vendita",
            ListingState.SOLD: "Venduto",
            ListingState.CLOSED: "Chiuso",
        }[self]


class OrderState(enum.StrEnum):
    """Where an order's money is.

    Named after the money rather than the parcel on purpose: every state
    transition here is a decision about who is holding value, and the shipping
    status is a detail of `Delivery`.
    """

    #: Captured into escrow, seller not yet paid.
    ESCROW = "escrow"
    SHIPPED = "shipped"
    #: Seller paid, buyer satisfied.
    COMPLETED = "completed"
    #: Frozen pending review. Nothing moves out of escrow in this state.
    DISPUTED = "disputed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderState.COMPLETED, OrderState.REFUNDED, OrderState.CANCELLED)

    @property
    def label(self) -> str:
        return {
            OrderState.ESCROW: "In garanzia",
            OrderState.SHIPPED: "Spedito",
            OrderState.COMPLETED: "Concluso",
            OrderState.DISPUTED: "In contestazione",
            OrderState.REFUNDED: "Rimborsato",
            OrderState.CANCELLED: "Annullato",
        }[self]


class DisputeState(enum.StrEnum):
    OPEN = "open"
    RESOLVED_BUYER = "resolved_buyer"
    RESOLVED_SELLER = "resolved_seller"
    WITHDRAWN = "withdrawn"

    @property
    def label(self) -> str:
        return {
            DisputeState.OPEN: "Aperta",
            DisputeState.RESOLVED_BUYER: "Risolta a favore dell'acquirente",
            DisputeState.RESOLVED_SELLER: "Risolta a favore del venditore",
            DisputeState.WITHDRAWN: "Ritirata",
        }[self]


# ---------------------------------------------------------------------------
# Escrow timing
# ---------------------------------------------------------------------------
# Escrow protects the buyer, but an escrow with no timeout punishes the seller
# for the buyer's silence. Every window here has an automatic end.

#: After the seller marks a parcel shipped, the buyer has this long to confirm
#: or dispute. Then the money is released automatically.
ESCROW_AUTO_RELEASE = timedelta(days=14)

#: A seller who never ships. After this the buyer is refunded without needing
#: to argue about it.
SHIPPING_DEADLINE = timedelta(days=10)

#: How long after completion a dispute may still be opened.
DISPUTE_WINDOW = timedelta(days=30)

#: A bet whose resolution source never speaks. Everybody gets their stake back
#: rather than the platform picking a winner.
BET_RESOLUTION_GRACE = timedelta(days=7)

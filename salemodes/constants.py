"""Vocabulary of the sale context.

Public: `marketplace` may import this module (and only this, `services` and
`registry`) to talk about sales without importing our models.
"""

from __future__ import annotations

import enum


class SaleState(enum.StrEnum):
    """Where a sale is in its life.

    THRESHOLD_MET is a distinct state rather than a boolean because it is the
    moment that unlocks a privilege — the seller may ask for an early draw — and
    a privilege guarded by a flag on a row is a privilege that will one day be
    granted by a stray `save()`.
    """

    OPEN = "open"
    THRESHOLD_MET = "threshold_met"
    #: Settlement in progress. Entered under a row lock so two concurrent draws
    #: cannot both pay out.
    SETTLING = "settling"
    SETTLED = "settled"
    ABORTED = "aborted"

    @property
    def is_terminal(self) -> bool:
        return self in (SaleState.SETTLED, SaleState.ABORTED)

    @property
    def accepts_participation(self) -> bool:
        return self in (SaleState.OPEN, SaleState.THRESHOLD_MET)

    @property
    def label(self) -> str:
        return {
            SaleState.OPEN: "Aperta",
            SaleState.THRESHOLD_MET: "Minimo raggiunto",
            SaleState.SETTLING: "Chiusura in corso",
            SaleState.SETTLED: "Conclusa",
            SaleState.ABORTED: "Annullata",
        }[self]


#: The only moves allowed. Anything else raises `IllegalStateTransition`.
ALLOWED_TRANSITIONS: dict[SaleState, frozenset[SaleState]] = {
    SaleState.OPEN: frozenset(
        {SaleState.THRESHOLD_MET, SaleState.SETTLING, SaleState.ABORTED}
    ),
    SaleState.THRESHOLD_MET: frozenset({SaleState.SETTLING, SaleState.ABORTED}),
    SaleState.SETTLING: frozenset({SaleState.SETTLED, SaleState.ABORTED}),
    SaleState.SETTLED: frozenset(),
    SaleState.ABORTED: frozenset(),
}


class ParticipationState(enum.StrEnum):
    """A participant's money, from their point of view."""

    #: Held. Still theirs, not spendable.
    ACTIVE = "active"
    #: Captured into escrow — they bought, or their ticket funded the pot.
    CAPTURED = "captured"
    #: Given back. Outbid, aborted below minimum, or refused by the mode.
    RELEASED = "released"

    @property
    def label(self) -> str:
        return {
            ParticipationState.ACTIVE: "In gioco",
            ParticipationState.CAPTURED: "Pagata",
            ParticipationState.RELEASED: "Rimborsata",
        }[self]


class FeeScope(enum.StrEnum):
    """Which participations end up paying the platform commission.

    POOL — everyone who joined pays (lottery, group buy, mystery box), so the fee
    is apportioned marginally as the pot grows and the parts sum to exactly the
    fee on the final pot.

    WINNER — only the winning participation is captured (auction, fixed price,
    challenge); the rest are released, so each participation carries its own
    self-contained fee.
    """

    POOL = "pool"
    WINNER = "winner"


# ---------------------------------------------------------------------------
# Anti-abuse policy
# ---------------------------------------------------------------------------

#: A seller who may draw whenever they like can wait for the instant at which
#: their confederate holds the largest share of the tickets. The announcement
#: window makes that instant unknowable: the draw is committed to publicly, and
#: anybody may keep buying tickets right up to it.
MINIMUM_EARLY_DRAW_NOTICE_MINUTES = 30
DEFAULT_EARLY_DRAW_NOTICE_MINUTES = 30

#: An announcement is binding. Without this, a seller could announce, watch who
#: joins, cancel, and announce again — shopping for a favourable moment one
#: window at a time. `settle_due_sales` draws an announced sale even if the
#: seller has gone quiet.
EARLY_DRAW_ANNOUNCEMENT_IS_BINDING = True

#: A bid inside this window pushes the close out. Sniping is not clever
#: bidding, it is a denial of the auction's own price discovery.
ANTI_SNIPE_WINDOW_SECONDS = 120
ANTI_SNIPE_EXTENSION_SECONDS = 120

#: Hard ceiling on extensions so an auction cannot be kept alive forever by two
#: bots trading one-scaglia increments.
MAX_ANTI_SNIPE_EXTENSIONS = 60

#: Holds outlive their sale by this much, so a stuck settlement does not freeze
#: a user's money for ever — `wallet.tasks.release_expired_holds` sweeps them.
HOLD_GRACE_DAYS = 3

#: Default life of a sale that does not declare one.
DEFAULT_SALE_DURATION_DAYS = 7

#: Purpose strings recorded on wallet holds. They show up on the user's
#: "impegnate" list, so they are user-facing and therefore Italian.
HOLD_PURPOSE = "Partecipazione a una vendita"
FEE_HOLD_PURPOSE = "Commissione marketplace"

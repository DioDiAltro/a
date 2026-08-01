"""Keys, states and tunables owned by the games context.

Nothing here crosses a boundary: every value is either a state machine label or
a product tuning knob. Economic constants that other contexts must agree with
(RTP, duel rake) live in `core.contracts` and are imported, never redefined.
"""

from __future__ import annotations

import enum
from datetime import timedelta


class GameKey(enum.StrEnum):
    """The registry keys. Stored on `GameRound.game_key` and used in URLs."""

    SLOT = "slot"
    WHEEL = "wheel"
    LADDER = "ladder"
    COINFLIP = "coinflip"
    #: Not a minigame the user picks — the round a duel resolves through.
    DUEL = "duel"


class RoundState(enum.StrEnum):
    """Where a round is in its life.

    `COMMITTED` exists as a distinct state from `SETTLED` because the fairness
    commitment is published before money moves: a round that crashed between
    commit and settlement must be visibly unfinished, not silently absent.
    """

    COMMITTED = "committed"
    #: The ladder, and only the ladder: the outcome is derived but the player
    #: has not yet decided how far to climb.
    IN_PROGRESS = "in_progress"
    SETTLED = "settled"
    CANCELLED = "cancelled"


class RoundSource(enum.StrEnum):
    """Why the round exists. Drives which settlement path applies."""

    CLAIM_GAMBLE = "claim_gamble"
    DUEL = "duel"


class DuelState(enum.StrEnum):
    PENDING = "pending"
    DECLINED = "declined"
    EXPIRED = "expired"
    RESOLVED = "resolved"

    @property
    def label(self) -> str:
        return {
            DuelState.PENDING: "In attesa di risposta",
            DuelState.DECLINED: "Rifiutata",
            DuelState.EXPIRED: "Scaduta",
            DuelState.RESOLVED: "Conclusa",
        }[self]


#: How long a challenge stays open before the challenger's hold is released.
#: Short enough that a stake is never frozen for a day the challenger did not
#: choose, long enough that a seller in a different timezone can answer.
DUEL_EXPIRY = timedelta(hours=12)

#: Anti-harassment: the same challenger may only come at the same defender this
#: many times in a rolling 24 hours. Duels are meant to be sport between
#: sellers; without this the mechanic is a channel for pestering someone until
#: they block you.
MAX_DUEL_CHALLENGES_PER_DAY = 3

#: A duel below this is not worth two people's attention and is a cheap way to
#: spam somebody's notification list.
MIN_DUEL_STAKE_MINOR = 100  # 1 biglia

# The *upper* bound on a duel stake is not defined here: `identity.levels` owns
# the level table and therefore owns `max_duel_stake`. Duplicating the ceiling in
# this module would guarantee the two drift, and the copy that lost would be the
# one deciding how much of a stranger's money a new account can put at risk.

#: Longest client seed we will store. Long enough for a wallet address or a
#: sentence the user cares about, short enough to keep the verify page honest.
CLIENT_SEED_MAX_LENGTH = 64

#: Bytes of entropy in a server seed. 32 bytes is 256 bits: guessing it before
#: reveal is the whole security of the commit-reveal scheme.
SERVER_SEED_BYTES = 32

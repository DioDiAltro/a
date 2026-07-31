"""XP and level progression.

Progression is not a cosmetic badge here: the level a seller has reached is what
caps the size of a duel they may accept. Handing a brand-new account the ability
to stake a thousand biglie against a stranger is how a platform gets used as a
transfer rail, so the cap starts small and is earned by activity that has
already been settled and audited.

Nothing in this module awards XP for *intent*. Every point comes from an event
in `core.contracts` that describes something that already happened and already
moved money — a settled sale, a settled game round, a claimed credit. That means
XP cannot be farmed by opening and cancelling things.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from core.contracts import CreditClaimed, GameSettled, SaleSettled
from core.events import bus
from core.money import Amount, Asset
from identity.models import PlayerLevel

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Level:
    number: int
    xp_required: int
    title: str
    #: Largest duel stake this level may put up, in minor units (scaglie).
    max_duel_stake_minor: int
    perks: tuple[str, ...] = ()

    @property
    def may_duel(self) -> bool:
        """Duels open at level 2. A fresh account cannot be challenged into one."""
        return self.number >= 2

    def max_duel_stake(self, asset: Asset = Asset.BIGLIA_PIENA) -> Amount:
        return Amount(self.max_duel_stake_minor, asset)


#: The table. Ordered by `number`; `xp_required` must be strictly increasing.
LEVELS: tuple[Level, ...] = (
    Level(1, 0, "Novizio", 500, ("Puoi comprare e vendere",)),
    Level(2, 100, "Apprendista", 1_500, ("Sfide tra venditori sbloccate",)),
    Level(3, 300, "Commerciante", 4_000, ("Puoi aprire lotterie",)),
    Level(4, 800, "Veterano", 10_000, ("Vetrina in evidenza",)),
    Level(5, 2_000, "Maestro", 25_000, ("Commissione ridotta sulle sfide",)),
    Level(6, 5_000, "Leggenda", 100_000, ("Nessun limite pratico sulle sfide",)),
)

# -- how much each settled thing is worth ------------------------------------
#: One point per whole biglia, so a €1 round and a €100 round are not equal, but
#: the curve is linear rather than exponential: grinding is possible, buying a
#: level outright is not.
XP_PER_BIGLIA = 1
XP_MINIMUM_PER_EVENT = 1
XP_SALE_COMPLETION_BONUS = 10
XP_RISKED_CLAIM_BONUS = 5
XP_PURCHASE_SHARE = 5


def level_for_xp(xp: int) -> Level:
    """The highest level whose threshold `xp` has reached."""
    reached = LEVELS[0]
    for level in LEVELS:
        if xp >= level.xp_required:
            reached = level
        else:
            break
    return reached


def next_level_after(number: int) -> Level | None:
    for level in LEVELS:
        if level.number == number + 1:
            return level
    return None


def level_of(user) -> PlayerLevel:
    """The user's progression row, created on first read if missing."""
    row, _ = PlayerLevel.objects.get_or_create(user=user)
    return row


def max_duel_stake(user, asset: Asset = Asset.BIGLIA_PIENA) -> Amount:
    """The largest duel stake this user may put up right now."""
    return level_of(user).definition.max_duel_stake(asset)


def can_stake_duel(user, amount: Amount) -> bool:
    """Whether `user` is allowed to duel for `amount`.

    `games` is expected to consult this before accepting a challenge; refusing
    late, after a hold is placed, is a worse experience and a worse audit trail.
    """
    definition = level_of(user).definition
    return definition.may_duel and amount.minor <= definition.max_duel_stake_minor


@transaction.atomic
def award_xp(user, points: int, *, reason: str) -> PlayerLevel:
    """Add XP and recompute the level. Returns the updated row.

    Locked, because two settled events landing at once must not read the same
    starting XP and each write their own total back.
    """
    if points <= 0:
        return level_of(user)

    row, _ = PlayerLevel.objects.select_for_update().get_or_create(user=user)
    row.xp += points
    new_level = level_for_xp(row.xp)

    changed = ["xp", "number", "updated_at"]
    if new_level.number != row.number:
        row.number = new_level.number
        row.leveled_up_at = timezone.now()
        changed.append("leveled_up_at")
        logger.info(
            "User %s reached level %s (%s) via %s", user.pk, new_level.number,
            new_level.title, reason,
        )

    row.save(update_fields=changed)
    return row


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _user(user_id):
    """Resolve a user id from an event, tolerating one that no longer exists."""
    if user_id is None:
        return None
    return get_user_model().objects.filter(pk=user_id).first()


def _points_for(minor: int) -> int:
    return max(XP_MINIMUM_PER_EVENT, abs(minor) // 100 * XP_PER_BIGLIA)


def on_sale_settled(event: SaleSettled) -> None:
    """A completed sale rewards the seller, and the buyer a little."""
    seller = _user(event.seller_id)
    if seller is not None:
        award_xp(
            seller,
            XP_SALE_COMPLETION_BONUS + _points_for(event.gross_minor),
            reason=f"sale:{event.sale_id}",
        )

    winner = _user(event.winner_id)
    if winner is not None:
        award_xp(winner, XP_PURCHASE_SHARE, reason=f"purchase:{event.sale_id}")


def on_game_settled(event: GameSettled) -> None:
    """XP tracks the stake, not the payout — losing is not punished twice."""
    user = _user(event.user_id)
    if user is not None:
        award_xp(user, _points_for(event.stake_minor), reason=f"game:{event.round_id}")


def on_credit_claimed(event: CreditClaimed) -> None:
    """Claiming is the core ritual, so it is the most reliable source of XP."""
    user = _user(event.user_id)
    if user is None:
        return
    points = _points_for(event.original_minor)
    if event.risked:
        points += XP_RISKED_CLAIM_BONUS
    award_xp(user, points, reason=f"claim:{event.credit_id}")


_registered = False


def register_handlers() -> None:
    """Subscribe to the events that grant XP. Called from `IdentityConfig.ready`."""
    global _registered
    if _registered:
        return
    bus.subscribe(SaleSettled, on_sale_settled)
    bus.subscribe(GameSettled, on_game_settled)
    bus.subscribe(CreditClaimed, on_credit_claimed)
    _registered = True

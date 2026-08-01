"""Who may play, for how much, and against whom.

Every rule that can refuse a wager lives here rather than being scattered
through `services`, because the failure mode of eligibility checks is that one
entry point forgets one of them. There are four entry points into this context
that move money — a claim gamble, a ladder step, a challenge and an accept —
and all four come through this module first.

The checks compose in a fixed order, and the order is not arbitrary:

1. **Self-exclusion** before anything else. Somebody who has excluded themselves
   must not learn whether they *would* have been allowed.
2. **Asset rights** next, since staking a non-wagerable asset is a programming
   error rather than a user-facing refusal.
3. **The player's own limits** last, because those are the ones whose refusal
   message is worth reading.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from compliance.constants import LimitKind
from compliance.services import assert_can_play, check_limit, record_usage
from core.exceptions import InvalidAmount, LimitExceeded
from core.money import Amount
from games.constants import MAX_DUEL_CHALLENGES_PER_DAY, MIN_DUEL_STAKE_MINOR
from identity.services import can_stake_duel, max_duel_stake


class DuelRefused(LimitExceeded):
    """A duel rule said no. A `LimitExceeded` because that is what it is to the
    person on the receiving end — and because the middleware and templates
    already know how to present one."""


# ---------------------------------------------------------------------------
# Wagering
# ---------------------------------------------------------------------------


def assert_may_wager(user, amount: Amount) -> None:
    """The gate in front of every stake this context takes.

    Both the wager limit and the loss limit are checked *before* the round, and
    both against the full stake. Checking the loss limit only after the fact
    would let a player whose loss limit has one biglia left stake a hundred:
    the amount at risk is the stake, so that is the number the limit has to see.
    """
    assert_can_play(user)

    if not amount.is_positive:
        raise InvalidAmount("Non si può puntare zero.")
    if not amount.asset.is_wagerable:
        raise InvalidAmount(f"{amount.asset.label} non si possono puntare.")

    check_limit(user, LimitKind.WAGER, amount)
    check_limit(user, LimitKind.LOSS, amount)


def record_wager(user, amount: Amount, *, reference: str = "") -> None:
    """Book the stake against the wager limit, after the round is committed."""
    record_usage(user, LimitKind.WAGER, amount, reference=reference)


def record_loss(user, staked: Amount, returned: Amount, *, reference: str = "") -> None:
    """Book the *net* loss, and only when there is one.

    Net, not gross: a player who staked 10 and got 8 back lost 2, and counting
    the full 10 against their loss limit would exhaust it at a speed that has
    nothing to do with what happened to their money.
    """
    net = staked - returned
    if net.is_positive:
        record_usage(user, LimitKind.LOSS, net, reference=reference)


# ---------------------------------------------------------------------------
# Duels
# ---------------------------------------------------------------------------


def seller_profile(user):
    """The user's selling identity, or None — without creating one.

    `identity.services.ensure_seller_profile` would answer this, but it
    get-or-creates, and a *read* on the duel page must not quietly enrol
    somebody as a seller who accepts duels. The reverse accessor is the only
    read-only route currently available; `identity.services` growing a
    `seller_profile(user)` selector would let this collapse to one call.
    """
    try:
        return user.seller_profile
    except ObjectDoesNotExist:
        return None


def accepts_duels(user) -> bool:
    profile = seller_profile(user)
    return profile is not None and profile.accepts_duels


def challenges_today(challenger, defender) -> int:
    from games.models import Duel

    since = timezone.now() - timedelta(days=1)
    return Duel.objects.recent_between(challenger, defender, since).count()


def assert_may_challenge(challenger, defender, stake: Amount) -> None:
    """Every rule that stands between wanting a duel and having one.

    The anti-harassment cap is the one that matters most and is the easiest to
    leave out: without it, "duels are opt-in" only protects the people who
    thought to opt out, and everyone else gets challenged until they do.
    """
    if challenger.pk == defender.pk:
        raise InvalidAmount("Non puoi sfidare te stesso.")

    assert_may_wager(challenger, stake)

    if stake.minor < MIN_DUEL_STAKE_MINOR:
        raise InvalidAmount(
            f"La posta minima per una sfida è {Amount(MIN_DUEL_STAKE_MINOR, stake.asset)}."
        )

    if seller_profile(challenger) is None:
        raise DuelRefused(
            limit_name="duel:not_a_seller",
            message="Le sfide sono tra venditori: apri la tua vetrina per lanciarne una.",
        )

    if not accepts_duels(defender):
        raise DuelRefused(
            limit_name="duel:opted_out",
            message="Questo venditore non accetta sfide.",
        )

    # The cap is the challenger's own level, checked before any hold is placed.
    if not can_stake_duel(challenger, stake):
        raise DuelRefused(
            limit_name="duel:level_cap",
            message=(
                f"Al tuo livello puoi sfidare fino a {max_duel_stake(challenger, stake.asset)}. "
                "Sali di livello per alzare il limite."
            ),
        )

    if challenges_today(challenger, defender) >= MAX_DUEL_CHALLENGES_PER_DAY:
        raise DuelRefused(
            limit_name="duel:harassment",
            message=(
                f"Hai già sfidato questa persona {MAX_DUEL_CHALLENGES_PER_DAY} volte "
                "nelle ultime 24 ore. Lasciala respirare."
            ),
        )


def assert_may_accept(defender, stake: Amount) -> None:
    """The defender's own gate.

    Deliberately a separate function: the defender never agreed to the
    challenger's level cap or the challenger's limits, and their eligibility has
    to be established at the moment they accept, not inferred from the fact that
    somebody was allowed to ask.
    """
    assert_may_wager(defender, stake)

    if not accepts_duels(defender):
        raise DuelRefused(
            limit_name="duel:opted_out",
            message="Hai disattivato le sfide. Riattivale dal tuo profilo per accettare.",
        )

    if not can_stake_duel(defender, stake):
        raise DuelRefused(
            limit_name="duel:level_cap",
            message=(
                f"Al tuo livello puoi sfidare fino a {max_duel_stake(defender, stake.asset)}."
            ),
        )

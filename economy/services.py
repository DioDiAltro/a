"""The economy context's public API: the drip, and the RTP watchdog.

The drip is example 2 from the brief — *"a user who is doing nothing can still
randomly earn a few biglie"* — and the only honest way to build it is as
**redistribution, never issuance**. Read literally it would be pure minting and
the fastest way to destroy the currency; implemented against the redistribution
pool the experience is identical (biglie rain on you for doing nothing) while
every biglia that rains was earned by the platform first. If the pool is empty,
nothing rains. A quiet week gives a thin drip, and that is correct.

That property is not enforced here. It is enforced by `wallet.services.drip`,
which returns None when the pool cannot cover the award. This module's job is to
decide *who*, *how much* and *how often* — and to stop one run from turning the
whole pool into a payroll.
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.contracts import DEFAULT_GAME_RTP_BASIS_POINTS
from core.money import Amount
from economy.constants import (
    DRIP_ASSET,
    DRIP_CANDIDATE_POOL,
    DRIP_DAILY_CAP_PER_USER_MINOR,
    DRIP_MAX_AWARD_MINOR,
    DRIP_MAX_RECIPIENTS_PER_RUN,
    DRIP_MIN_ACCOUNT_AGE,
    DRIP_MIN_AWARD_MINOR,
    DRIP_RUN_BUDGET_BASIS_POINTS,
    DRIP_RUN_MAX_MINOR,
    DRIP_STALE_AFTER,
    DRIP_USER_COOLDOWN,
    DRIP_WEIGHT_HOURS_CAP,
    DRIP_WEIGHT_HOURS_PER_STEP,
    RTP_AUDIT_WINDOW,
    RTP_DRIFT_TOLERANCE_BP,
    RTP_MIN_ROUNDS_FOR_ALERT,
)
from economy.models import DripAward, GameRoundOutcome, RtpAudit
from identity.services import MINIMUM_AGE
from wallet.selectors import redistribution_pool_balance
from wallet.services import drip as wallet_drip

logger = logging.getLogger("economy")


# ---------------------------------------------------------------------------
# The passive drip
# ---------------------------------------------------------------------------


def _majority_cutoff(today: date) -> date:
    """The latest birth date that is still eighteen years ago."""
    try:
        return today.replace(year=today.year - MINIMUM_AGE)
    except ValueError:  # 29 February
        return today.replace(year=today.year - MINIMUM_AGE, day=28)


def eligible_users(*, now=None):
    """Everyone the drip may rain on, as a queryset.

    Four fences, and each one is a lesson rather than a preference:

    * of age, active and not self-excluded — an unsolicited reward landing in an
      excluded player's account is the platform soliciting them, which is the
      exact thing self-exclusion exists to stop;
    * account older than a few days — otherwise the drip pays whoever can create
      email addresses fastest;
    * seen recently — money parked in abandoned accounts is a liability that
      never becomes anything;
    * not dripped on in the last few hours — so a run cannot follow the previous
      run straight back to the same account.
    """
    now = now or timezone.now()
    User = get_user_model()

    recently_dripped = DripAward.objects.filter(
        awarded_at__gte=now - DRIP_USER_COOLDOWN
    ).values_list("user_id", flat=True)

    return (
        User.objects.filter(
            is_active=True,
            is_self_excluded=False,
            date_of_birth__isnull=False,
            date_of_birth__lte=_majority_cutoff(timezone.localdate()),
            date_joined__lte=now - DRIP_MIN_ACCOUNT_AGE,
        )
        # `last_seen_at` is only set once something touches it; falling back to
        # the join date means a brand-new-but-idle account is still a candidate
        # rather than silently excluded forever.
        .annotate(active_at=Coalesce("last_seen_at", "date_joined"))
        .filter(active_at__gte=now - DRIP_STALE_AFTER)
        .exclude(pk__in=recently_dripped)
    )


def _selection_weight(user, last_award_at, now) -> int:
    """Longer since your last drip, likelier your name comes up.

    Weighted rather than uniform so that the reward spreads out over the player
    base instead of clustering, and capped so a dormant account is favoured but
    never certain. Certainty would make it a schedule, and the feature is
    supposed to feel like luck.
    """
    since = last_award_at or getattr(user, "last_seen_at", None) or user.date_joined
    hours = max(0.0, (now - since).total_seconds() / 3600)
    return 1 + int(min(hours, DRIP_WEIGHT_HOURS_CAP) // DRIP_WEIGHT_HOURS_PER_STEP)


def _weighted_sample(pairs: list[tuple], k: int, rng: random.Random) -> list[tuple]:
    """Pick `k` distinct entries, probability proportional to weight."""
    pool = list(pairs)
    chosen: list[tuple] = []

    for _ in range(min(k, len(pool))):
        total = sum(weight for _, weight in pool)
        if total <= 0:
            break
        target = rng.random() * total
        running = 0.0
        for index, (_candidate, weight) in enumerate(pool):
            running += weight
            if target <= running:
                chosen.append(pool.pop(index))
                break

    return chosen


def _daily_headroom(user, now) -> int:
    """What this user may still receive today, in minor units."""
    spent = (
        DripAward.objects.filter(user=user, awarded_at__gte=now - timedelta(days=1))
        .aggregate(total=Sum("amount_minor"))["total"]
        or 0
    )
    return max(0, DRIP_DAILY_CAP_PER_USER_MINOR - spent)


def run_drip(
    *,
    now=None,
    rng: random.Random | None = None,
    budget_minor: int | None = None,
    max_recipients: int | None = None,
) -> dict:
    """One pass of the passive drip. Returns a summary of what it did.

    Never mints: every award goes through `wallet.services.drip`, which pays out
    of REDISTRIBUTION_POOL and refuses when the pool is short. If it refuses
    mid-run the run stops rather than trying smaller amounts — a pool that just
    ran out is a pool that should stay empty until revenue refills it.
    """
    now = now or timezone.now()
    rng = rng or random.Random()
    run_id = uuid.uuid4()

    pool = redistribution_pool_balance(DRIP_ASSET)
    if pool.minor < DRIP_MIN_AWARD_MINOR:
        return _summary(run_id, pool, 0, [], skipped="pool_empty")

    budget = min(
        budget_minor if budget_minor is not None else
        pool.minor * DRIP_RUN_BUDGET_BASIS_POINTS // 10_000,
        DRIP_RUN_MAX_MINOR,
        pool.minor,
    )
    if budget < DRIP_MIN_AWARD_MINOR:
        return _summary(run_id, pool, budget, [], skipped="budget_below_minimum")

    candidates = list(eligible_users(now=now).order_by("?")[:DRIP_CANDIDATE_POOL])
    if not candidates:
        return _summary(run_id, pool, budget, [], skipped="no_eligible_users")

    last_awards = _last_award_map(candidates)
    weighted = [
        (user, _selection_weight(user, last_awards.get(user.pk), now)) for user in candidates
    ]
    limit = max_recipients if max_recipients is not None else DRIP_MAX_RECIPIENTS_PER_RUN
    chosen = _weighted_sample(weighted, limit, rng)

    awarded: list[DripAward] = []
    remaining = budget

    for user, weight in chosen:
        if remaining < DRIP_MIN_AWARD_MINOR:
            break

        # Belt and braces: the queryset already filtered on the same conditions,
        # but `can_wager` is the single predicate the rest of the platform
        # consults and it must be the one that decides here too.
        if not user.can_wager:
            continue

        headroom = _daily_headroom(user, now)
        if headroom < DRIP_MIN_AWARD_MINOR:
            continue

        amount_minor = min(
            rng.randint(DRIP_MIN_AWARD_MINOR, DRIP_MAX_AWARD_MINOR), headroom, remaining
        )
        if amount_minor < DRIP_MIN_AWARD_MINOR:
            continue

        credit = wallet_drip(
            user=user,
            amount=Amount(amount_minor, DRIP_ASSET),
            idempotency_key=f"drip:{run_id}:{user.pk}",
        )
        if credit is None:
            # The pool went dry underneath us. Stop; do not retry smaller.
            logger.info("Drip run %s stopped early: pool exhausted", run_id)
            break

        awarded.append(
            DripAward.objects.create(
                run_id=run_id,
                user=user,
                amount_minor=amount_minor,
                asset=str(DRIP_ASSET),
                credit_id=credit.pk,
                weight=weight,
                awarded_at=now,
            )
        )
        remaining -= amount_minor

    summary = _summary(run_id, pool, budget, awarded)
    logger.info(
        "Drip run %s: %s awards totalling %s of a %s budget",
        run_id, summary["recipients"], summary["awarded_minor"], budget,
    )
    return summary


def _last_award_map(users) -> dict:
    """user_id -> most recent award time, for the weighting."""
    rows = (
        DripAward.objects.filter(user__in=users)
        .values("user_id")
        .annotate(last=Max("awarded_at"))
    )
    return {row["user_id"]: row["last"] for row in rows}


def _summary(run_id, pool: Amount, budget_minor: int, awarded: list, skipped: str = "") -> dict:
    return {
        "run_id": str(run_id),
        "asset": str(DRIP_ASSET),
        "pool_minor": pool.minor,
        "budget_minor": budget_minor,
        "awarded_minor": sum(a.amount_minor for a in awarded),
        "recipients": len(awarded),
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# RTP surveillance
# ---------------------------------------------------------------------------


def configured_rtp_bp(game_key: str) -> int:
    """The RTP a game promises, in basis points.

    `games` declares this on each definition, but `economy` may not import
    `games` (tests/test_boundaries.py) and `GameSettled` does not carry the
    figure. So the reference comes from settings, then from this context's own
    override map, then from the platform default in `core.contracts`.
    """
    from django.conf import settings

    from economy.constants import CONFIGURED_RTP_BP

    overrides = {**CONFIGURED_RTP_BP, **getattr(settings, "ECONOMY_CONFIGURED_RTP_BP", {})}
    return int(overrides.get(str(game_key), DEFAULT_GAME_RTP_BASIS_POINTS))


def audit_rtp(*, window: timedelta | None = None, now=None) -> list[RtpAudit]:
    """Compare realised against configured RTP for every game that ran.

    A drift is not a rounding curiosity: the outcome tables are the promise made
    to the player, and a game paying materially less than it advertises is the
    platform lying. So the alert fires in both directions — paying too much is a
    bug that costs money, paying too little is a bug that costs trust.
    """
    now = now or timezone.now()
    window = window or RTP_AUDIT_WINDOW
    start = now - window

    rows = (
        GameRoundOutcome.objects.filter(settled_at__gte=start, settled_at__lte=now)
        .values("game_key")
        .annotate(
            rounds=Count("id"),
            staked=Sum("stake_minor"),
            paid=Sum("payout_minor"),
        )
        .order_by("game_key")
    )

    audits: list[RtpAudit] = []
    for row in rows:
        staked = row["staked"] or 0
        paid = row["paid"] or 0
        if staked <= 0:
            continue

        realised_bp = paid * 10_000 // staked
        configured = configured_rtp_bp(row["game_key"])
        drift = realised_bp - configured
        significant = row["rounds"] >= RTP_MIN_ROUNDS_FOR_ALERT
        is_alert = significant and abs(drift) > RTP_DRIFT_TOLERANCE_BP

        audit = RtpAudit.objects.create(
            game_key=row["game_key"],
            window_start=start,
            window_end=now,
            rounds=row["rounds"],
            staked_minor=staked,
            paid_minor=paid,
            realised_bp=realised_bp,
            configured_bp=configured,
            drift_bp=drift,
            is_alert=is_alert,
            note="" if significant else "Campione troppo piccolo per un allarme.",
        )
        audits.append(audit)

        if is_alert:
            logger.error(
                "RTP drift on %s: realised %.2f%% against configured %.2f%% over "
                "%d rounds (%d staked, %d paid). An outcome table is wrong — the "
                "tables are the promise made to the player.",
                audit.game_key, audit.realised_percent, audit.configured_percent,
                audit.rounds, staked, paid,
            )

    return audits

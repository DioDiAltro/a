"""The compliance context's public API — the enforcement layer.

Everything here says *no* for a living. The models hold the state; this module
is where a deposit gets refused, a withdrawal gets gated, and an exclusion
refuses to be lifted early.

Two design commitments worth stating, because both cost something and both are
deliberate:

* **The asymmetry is enforced here, not in a form.** Lowering a limit takes
  effect immediately; raising one waits 24 hours. A limit a user can raise the
  instant they hit it is a suggestion, and the moment someone wants to raise it
  is exactly the moment they should not be able to.

* **Nothing is silently allowed.** Every check either passes or raises a typed
  exception carrying an Italian message the user can act on. There is no code
  path that shrugs and lets the money through.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from compliance.constants import (
    COOLING_OFF,
    FULL_KYC_CUMULATIVE_THRESHOLD_MINOR,
    MONEY_LIMIT_KINDS,
    PERIOD_WINDOW,
    TRACKED_LIMIT_KINDS,
    WITHDRAWAL_MINIMUM_TIER,
    ExclusionState,
    KycStatus,
    KycTier,
    LimitKind,
    LimitPeriod,
    tier_at_least,
    tier_rank,
)
from compliance.models import (
    KycVerification,
    LimitUsage,
    PlayerLimit,
    RealityCheckSetting,
    SelfExclusion,
)
from core.contracts import SelfExclusionStarted
from core.events import bus
from core.exceptions import (
    IllegalStateTransition,
    KycRequired,
    LimitExceeded,
    SelfExcluded,
)
from core.money import Amount

logger = logging.getLogger("compliance.audit")


# ---------------------------------------------------------------------------
# KYC
# ---------------------------------------------------------------------------


def current_tier(user) -> str:
    """The highest tier this user currently holds an effective verification for."""
    now = timezone.now()
    best = KycTier.NONE

    for verification in KycVerification.objects.filter(
        user=user, status=KycStatus.APPROVED
    ).only("tier", "expires_at", "status"):
        if verification.is_effective(now) and tier_rank(verification.tier) > tier_rank(best):
            best = verification.tier

    return best


def cumulative_withdrawn_minor(user) -> int:
    """Lifetime withdrawals, used to decide when BASIC stops being enough.

    Counted under its own kind rather than piggybacking on DEPOSIT rows. An
    earlier version filtered deposit usage by a `withdrawal:` reference prefix,
    which forced `payments` to file withdrawals as deposits — so taking €500 out
    silently consumed €500 of the user's *deposit* ceiling. Tracking and capping
    are different questions and now have different kinds.
    """
    total = LimitUsage.objects.filter(
        user=user, kind=LimitKind.WITHDRAWAL
    ).aggregate(total=Sum("amount_minor"))["total"]
    return total or 0


def required_tier_for_withdrawal(user, amount: Amount) -> str:
    """Which tier this particular withdrawal demands.

    Threshold-based rather than flat: asking for a passport to move €10 drives
    people to worse platforms, and asking for nothing to move €10,000 is how a
    platform becomes a laundry.
    """
    projected = cumulative_withdrawn_minor(user) + amount.minor
    if projected >= FULL_KYC_CUMULATIVE_THRESHOLD_MINOR:
        return KycTier.FULL
    return WITHDRAWAL_MINIMUM_TIER


def require_kyc_for_withdrawal(user, amount: Amount) -> None:
    """Raise `KycRequired` unless this user may withdraw this amount."""
    required = required_tier_for_withdrawal(user, amount)
    held = current_tier(user)

    if not tier_at_least(held, required):
        if required == KycTier.FULL:
            raise KycRequired(
                "Superata la soglia cumulativa: serve la verifica completa per "
                "prelevare questo importo."
            )
        raise KycRequired("Verifica la tua identità prima di prelevare.")


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


def effective_limit(user, kind: LimitKind, period: LimitPeriod) -> int | None:
    """The limit in force right now, honouring any pending raise. None = unset."""
    limit = PlayerLimit.objects.filter(user=user, kind=kind, period=period).first()
    return None if limit is None else limit.effective_value()


def usage_in_window(user, kind: LimitKind, period: LimitPeriod) -> int:
    since = timezone.now() - PERIOD_WINDOW[period]
    total = LimitUsage.objects.filter(
        user=user, kind=kind, occurred_at__gte=since
    ).aggregate(total=Sum("amount_minor"))["total"]
    return total or 0


def check_limit(user, kind: LimitKind, amount: Amount) -> None:
    """Raise `LimitExceeded` if this action would breach a limit the user set.

    Checked across every period the user configured, not just the shortest: a
    daily limit alone lets someone lose their monthly limit in four days.
    """
    if kind not in MONEY_LIMIT_KINDS:
        raise ValueError(f"{kind} is not a money limit; use check_session_time.")

    for period in LimitPeriod.values:
        ceiling = effective_limit(user, kind, period)
        if ceiling is None:
            continue

        if usage_in_window(user, kind, period) + amount.minor > ceiling:
            logger.info(
                "limit_breached user=%s kind=%s period=%s attempted=%s",
                user.pk, kind, period, amount.minor,
            )
            raise LimitExceeded(
                limit_name=f"{kind}:{period}",
                message=(
                    f"Questa operazione supererebbe il limite {LimitPeriod(period).label.lower()} "
                    f"che hai impostato."
                ),
            )


def record_usage(user, kind: LimitKind, amount: Amount, *, reference: str = "") -> None:
    """Log consumption against a limit. Call *after* the action succeeds.

    Accepts WITHDRAWAL as well as the cappable kinds — it is recorded for the
    cumulative KYC threshold and never enforced as a ceiling.
    """
    if kind not in TRACKED_LIMIT_KINDS and kind != LimitKind.SESSION_TIME:
        raise ValueError(f"{kind} is not a tracked usage kind.")

    LimitUsage.objects.create(
        user=user, kind=kind, amount_minor=amount.minor,
        asset=amount.asset, reference=reference,
    )


@transaction.atomic
def set_limit(user, kind: LimitKind, period: LimitPeriod, value: int) -> PlayerLimit:
    """Set or change a limit, applying the cooling-off asymmetry.

    Lowering (or setting for the first time) is immediate. Raising is scheduled
    24 hours out and can be cancelled in the meantime — which is the entire
    point: the decision to loosen a limit should be made by tomorrow's calmer
    version of the person who made it.
    """
    limit, created = PlayerLimit.objects.select_for_update().get_or_create(
        user=user, kind=kind, period=period, defaults={"value": value},
    )
    if created:
        return limit

    if value <= limit.effective_value():
        limit.value = value
        limit.pending_value = None
        limit.effective_from = None
    else:
        limit.pending_value = value
        limit.effective_from = timezone.now() + COOLING_OFF

    limit.save()
    return limit


@transaction.atomic
def cancel_pending_limit_change(user, kind: LimitKind, period: LimitPeriod) -> None:
    """Abandon a scheduled raise. Always allowed, always immediate."""
    PlayerLimit.objects.filter(user=user, kind=kind, period=period).update(
        pending_value=None, effective_from=None
    )


# ---------------------------------------------------------------------------
# Self-exclusion
# ---------------------------------------------------------------------------


def active_exclusion(user) -> SelfExclusion | None:
    now = timezone.now()
    for exclusion in SelfExclusion.objects.filter(user=user, state=ExclusionState.ACTIVE):
        if exclusion.is_active(now):
            return exclusion
    return None


@transaction.atomic
def start_self_exclusion(user, *, until=None, reason: str = "") -> SelfExclusion:
    """Pull the cord.

    Publishes `SelfExclusionStarted` so every other context can cancel open
    sales, release staked wagers and stop notifying. Blocking login is the least
    of what self-exclusion has to mean.
    """
    existing = active_exclusion(user)
    if existing is not None:
        return existing

    exclusion = SelfExclusion.objects.create(user=user, until=until, reason=reason)

    user.is_self_excluded = True
    user.save(update_fields=["is_self_excluded"])

    logger.info("self_exclusion_started user=%s until=%s", user.pk, until)
    bus.publish(SelfExclusionStarted(user_id=user.id, until=until))
    return exclusion


@transaction.atomic
def end_self_exclusion(user) -> None:
    """Lift an exclusion whose term has expired.

    Refuses while the term is still running — that refusal is the feature. A
    self-exclusion that can be cancelled at 2am by the person who set it at
    noon protects nobody.
    """
    exclusion = active_exclusion(user)
    if exclusion is None:
        user.is_self_excluded = False
        user.save(update_fields=["is_self_excluded"])
        return

    if not exclusion.is_due_to_end():
        raise IllegalStateTransition(
            current="self_excluded",
            attempted="end_self_exclusion",
            message=(
                "L'autoesclusione non può essere revocata prima della scadenza. "
                "È così apposta."
            ),
        )

    exclusion.state = ExclusionState.EXPIRED
    exclusion.ended_at = timezone.now()
    exclusion.save(update_fields=["state", "ended_at"])

    user.is_self_excluded = False
    user.save(update_fields=["is_self_excluded"])


def assert_can_play(user) -> None:
    """The gate every wagering entry point calls before taking a stake."""
    if user.is_self_excluded or active_exclusion(user) is not None:
        raise SelfExcluded()
    if not user.can_wager:
        raise SelfExcluded("Il tuo conto non può giocare in questo momento.")


# ---------------------------------------------------------------------------
# Reality check
# ---------------------------------------------------------------------------


def reality_check_due(user, session) -> bool:
    """Whether it is time to tell the user how long they have been playing."""
    setting = RealityCheckSetting.objects.filter(user=user).first()
    if setting is None or not setting.enabled:
        return False

    last = setting.last_notified_at
    if last is None:
        return True
    return timezone.now() - last >= timedelta(minutes=setting.interval_minutes)


def mark_reality_check_shown(user) -> None:
    RealityCheckSetting.objects.filter(user=user).update(last_notified_at=timezone.now())

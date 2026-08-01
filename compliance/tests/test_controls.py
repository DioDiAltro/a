"""The controls that have to work.

Everything in this file is a refusal. If one of these tests goes green while the
behaviour is gone, the platform still functions perfectly and has stopped
protecting anybody — which is exactly the failure mode that never shows up in a
bug report.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from freezegun import freeze_time

from compliance.constants import (
    FULL_KYC_CUMULATIVE_THRESHOLD_MINOR,
    KycStatus,
    KycTier,
    LimitKind,
    LimitPeriod,
)
from compliance.models import AuditEvent, KycVerification
from compliance.services import (
    active_exclusion,
    cancel_pending_limit_change,
    check_limit,
    current_tier,
    effective_limit,
    end_self_exclusion,
    record_usage,
    require_kyc_for_withdrawal,
    set_limit,
    start_self_exclusion,
)
from core.exceptions import IllegalStateTransition, KycRequired, LimitExceeded, SelfExcluded
from core.money import Amount, Asset

PIENA = Asset.BIGLIA_PIENA


def biglie(units: str) -> Amount:
    return Amount.from_major(units, PIENA)


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(
        email="p@example.com", password="x" * 12, date_of_birth=date(1990, 1, 1)
    )


def approve_kyc(user, tier: KycTier) -> KycVerification:
    return KycVerification.objects.create(
        user=user, tier=tier, status=KycStatus.APPROVED, reviewed_at=timezone.now()
    )


# ---------------------------------------------------------------------------
# Limits: the asymmetry
# ---------------------------------------------------------------------------


def test_lowering_a_limit_is_immediate(db, user):
    set_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY, 10_000)
    set_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY, 2_000)

    assert effective_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY) == 2_000


def test_raising_a_limit_waits_a_day(db, user):
    """The single most important behaviour in this context."""
    with freeze_time("2026-03-01 12:00:00"):
        set_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY, 2_000)
        set_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY, 50_000)

        # Still on the old, lower ceiling.
        assert effective_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY) == 2_000

    with freeze_time("2026-03-02 11:59:00"):
        assert effective_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY) == 2_000

    with freeze_time("2026-03-02 12:01:00"):
        assert effective_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY) == 50_000


def test_a_pending_raise_can_be_cancelled(db, user):
    with freeze_time("2026-03-01 12:00:00"):
        set_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY, 2_000)
        set_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY, 90_000)
        cancel_pending_limit_change(user, LimitKind.DEPOSIT, LimitPeriod.DAY)

    with freeze_time("2026-03-05 12:00:00"):
        assert effective_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY) == 2_000


def test_limit_blocks_the_action_that_would_breach_it(db, user):
    set_limit(user, LimitKind.DEPOSIT, LimitPeriod.DAY, biglie("100").minor)
    record_usage(user, LimitKind.DEPOSIT, biglie("80"), reference="dep-1")

    check_limit(user, LimitKind.DEPOSIT, biglie("20"))  # exactly at the ceiling: fine

    with pytest.raises(LimitExceeded):
        check_limit(user, LimitKind.DEPOSIT, biglie("21"))


def test_a_longer_period_still_binds(db, user):
    """A daily limit alone lets someone lose a monthly limit in four days."""
    set_limit(user, LimitKind.LOSS, LimitPeriod.DAY, biglie("50").minor)
    set_limit(user, LimitKind.LOSS, LimitPeriod.MONTH, biglie("120").minor)
    record_usage(user, LimitKind.LOSS, biglie("110"), reference="loss-1")

    with pytest.raises(LimitExceeded):
        check_limit(user, LimitKind.LOSS, biglie("20"))


# ---------------------------------------------------------------------------
# Self-exclusion
# ---------------------------------------------------------------------------


def test_self_exclusion_cannot_be_lifted_early(db, user):
    start_self_exclusion(user, until=timezone.now() + timedelta(days=30))
    user.refresh_from_db()
    assert user.is_self_excluded

    with pytest.raises(IllegalStateTransition):
        end_self_exclusion(user)

    user.refresh_from_db()
    assert user.is_self_excluded


def test_self_exclusion_lifts_once_the_term_is_over(db, user):
    with freeze_time("2026-03-01 12:00:00"):
        start_self_exclusion(user, until=timezone.now() + timedelta(days=7))

    with freeze_time("2026-03-09 12:00:00"):
        end_self_exclusion(user)

    user.refresh_from_db()
    assert not user.is_self_excluded
    assert active_exclusion(user) is None


def test_permanent_exclusion_never_lifts(db, user):
    start_self_exclusion(user, until=None)

    with freeze_time("2036-01-01 12:00:00"):
        with pytest.raises(IllegalStateTransition):
            end_self_exclusion(user)


def test_excluded_user_cannot_wager(db, user):
    from compliance.services import assert_can_play

    assert_can_play(user)  # fine before

    start_self_exclusion(user, until=timezone.now() + timedelta(days=1))
    user.refresh_from_db()

    with pytest.raises(SelfExcluded):
        assert_can_play(user)


def test_underage_user_cannot_wager(db):
    from compliance.services import assert_can_play

    minor = get_user_model().objects.create_user(
        email="kid@example.com", password="x" * 12,
        date_of_birth=timezone.localdate() - timedelta(days=365 * 15),
    )
    with pytest.raises(SelfExcluded):
        assert_can_play(minor)


# ---------------------------------------------------------------------------
# KYC
# ---------------------------------------------------------------------------


def test_withdrawal_needs_at_least_basic_verification(db, user):
    assert current_tier(user) == KycTier.NONE

    with pytest.raises(KycRequired):
        require_kyc_for_withdrawal(user, biglie("50"))

    approve_kyc(user, KycTier.BASIC)
    require_kyc_for_withdrawal(user, biglie("50"))  # now allowed


def test_large_cumulative_withdrawals_require_full_verification(db, user):
    approve_kyc(user, KycTier.BASIC)

    over_threshold = Amount(FULL_KYC_CUMULATIVE_THRESHOLD_MINOR + 1, PIENA)
    with pytest.raises(KycRequired):
        require_kyc_for_withdrawal(user, over_threshold)

    approve_kyc(user, KycTier.FULL)
    require_kyc_for_withdrawal(user, over_threshold)


def test_expired_verification_stops_counting(db, user):
    KycVerification.objects.create(
        user=user, tier=KycTier.BASIC, status=KycStatus.APPROVED,
        reviewed_at=timezone.now(), expires_at=timezone.now() - timedelta(days=1),
    )
    assert current_tier(user) == KycTier.NONE


# ---------------------------------------------------------------------------
# The audit journal
# ---------------------------------------------------------------------------


def test_audit_chain_detects_a_tampered_row(db, user):
    """Hash chaining is what makes the journal worth keeping.

    A row rewritten behind the ORM stops matching its own hash, and every row
    after it stops matching too — so tampering is detectable rather than merely
    unlikely.
    """
    first = AuditEvent.record(event_name="test.one", user_id=user.pk, payload={"n": 1})
    second = AuditEvent.record(event_name="test.two", user_id=user.pk, payload={"n": 2})

    assert first.is_intact()
    assert second.is_intact()
    assert second.previous_hash == first.hash_chain

    # Rewrite history behind the ORM's back, the way an attacker with database
    # access would.
    AuditEvent.objects.filter(pk=first.pk).update(payload={"n": 999})
    first.refresh_from_db()

    assert not first.is_intact()


def test_audit_rows_refuse_to_be_updated_through_the_orm(db, user):
    event = AuditEvent.record(event_name="test.three", user_id=user.pk, payload={})

    event.event_name = "test.rewritten"
    with pytest.raises(IllegalStateTransition):
        event.save()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_a_real_money_movement_lands_in_the_journal(db, user, django_capture_on_commit_callbacks):
    """End to end: wallet publishes, compliance records, nobody imported anybody.

    This is the test that proves the event bus is actually load-bearing rather
    than decorative — `wallet` has no idea `compliance` exists.
    """
    from wallet.constants import CreditSource
    from wallet.services import claim_safe, credit_pending

    with django_capture_on_commit_callbacks(execute=True):
        credit = credit_pending(
            user=user, amount=biglie("40"), source=CreditSource.GAME_WIN,
            idempotency_key="audit-1", reference_id="audit-1",
        )
        claim_safe(credit=credit, idempotency_key="audit-claim-1")

    recorded = AuditEvent.objects.filter(
        event_name="wallet.credit_claimed", user_id=user.pk
    )
    assert recorded.count() == 1
    assert AuditEvent.verify_chain() == []

"""The passive drip: it must feel like luck and it must never mint.

`wallet/tests/test_services.py` already proves the ledger primitive — one drip
call against an empty pool returns None. These tests are about the *run*: who it
picks, how much it is allowed to hand out, and the fact that a scheduled job
firing every five minutes cannot turn a funded pool into a payroll.

The invariant under all of it is `docs/ECONOMY.md`: the drip is redistribution.
Every test that asserts on circulating supply is asserting that sentence.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.money import Amount, Asset
from economy.constants import (
    DRIP_ASSET,
    DRIP_DAILY_CAP_PER_USER_MINOR,
    DRIP_MAX_AWARD_MINOR,
    DRIP_MIN_ACCOUNT_AGE,
    DRIP_RUN_MAX_MINOR,
)
from economy.models import DripAward
from economy.services import eligible_users, run_drip
from economy.tasks import run_passive_drip
from wallet.constants import AccountKind
from wallet.ledger import circulating_supply, reconcile, system_account
from wallet.models import PendingCredit
from wallet.services import recognise_revenue

VINTA = Asset.BIGLIA_VINTA


def biglie(units: str, asset: Asset = VINTA) -> Amount:
    return Amount.from_major(units, asset)


def make_user(email: str, *, joined_days_ago: int = 10, **extra):
    """A user old enough and awake enough to be dripped on."""
    user = get_user_model().objects.create_user(
        email=email,
        password="x" * 12,
        date_of_birth=extra.pop("date_of_birth", date(1990, 1, 1)),
        **extra,
    )
    when = timezone.now() - timedelta(days=joined_days_ago)
    get_user_model().objects.filter(pk=user.pk).update(date_joined=when, last_seen_at=when)
    user.refresh_from_db()
    return user


@pytest.fixture
def players(db):
    return [make_user(f"p{index}@example.com") for index in range(6)]


def fund_pool(amount: Amount = None) -> Amount:
    """Recognise revenue so 10% lands in the redistribution pool."""
    recognise_revenue(amount=amount or biglie("10000"), source="test")
    return system_account(AccountKind.REDISTRIBUTION_POOL, VINTA).balance


# ---------------------------------------------------------------------------
# The line that keeps it non-inflationary
# ---------------------------------------------------------------------------


def test_a_run_against_a_dry_pool_pays_nothing_and_mints_nothing(db, players):
    """The whole feature, and the reason it is safe, in one test."""
    supply_before = circulating_supply(VINTA)

    summary = run_drip(rng=random.Random(1))

    assert summary["recipients"] == 0
    assert summary["awarded_minor"] == 0
    assert summary["skipped"] == "pool_empty"
    assert circulating_supply(VINTA) == supply_before
    assert not DripAward.objects.exists()
    assert not PendingCredit.objects.exists()


def test_a_funded_run_redistributes_without_minting(db, players):
    """Biglie rain, and the total quantity of biglie does not change.

    This is the difference between the brief read literally ("users randomly
    earn biglie") and the brief implemented ("users randomly *receive* biglie
    the platform already earned"). The user experience is identical; one of the
    two destroys the currency.
    """
    fund_pool()
    supply_before = circulating_supply(VINTA)

    summary = run_drip(rng=random.Random(7))

    assert summary["recipients"] > 0
    assert summary["awarded_minor"] > 0
    assert circulating_supply(VINTA) == supply_before
    assert reconcile(record_anomalies=False) == []


def test_the_pool_shrinks_by_exactly_what_was_awarded(db, players):
    pool_before = fund_pool()

    summary = run_drip(rng=random.Random(3))

    pool_after = system_account(AccountKind.REDISTRIBUTION_POOL, VINTA).balance
    assert pool_before.minor - pool_after.minor == summary["awarded_minor"]


def test_awards_land_as_unclaimed_credits(db, players):
    """Even free money goes through the claim ritual — no `credit_available`."""
    fund_pool()

    run_drip(rng=random.Random(11))

    awards = list(DripAward.objects.all())
    assert awards
    for award in awards:
        credit = PendingCredit.objects.get(pk=award.credit_id)
        assert credit.state == "pending"
        assert credit.asset == DRIP_ASSET


# ---------------------------------------------------------------------------
# The caps
# ---------------------------------------------------------------------------


def test_one_run_cannot_drain_the_pool(db, players):
    """Beat fires this every five minutes; without the budget cap the first
    tick after a busy day would empty the pool into a handful of accounts."""
    pool_before = fund_pool(biglie("100000"))

    summary = run_drip(rng=random.Random(5))

    assert summary["budget_minor"] <= DRIP_RUN_MAX_MINOR
    assert summary["awarded_minor"] <= summary["budget_minor"]
    assert summary["awarded_minor"] < pool_before.minor // 2


def test_no_single_award_exceeds_the_ceiling(db, players):
    fund_pool()

    run_drip(rng=random.Random(13))

    assert all(a.amount_minor <= DRIP_MAX_AWARD_MINOR for a in DripAward.objects.all())
    assert all(a.amount_minor >= 1 for a in DripAward.objects.all())


def test_the_daily_cap_binds_across_runs(db):
    """A user the draw keeps favouring still stops at one biglia a day."""
    lucky = make_user("lucky@example.com")
    fund_pool(biglie("100000"))
    now = timezone.now()

    # Many runs, one candidate, cooldown neutralised by advancing the clock a
    # little each time — the daily cap is the only thing left holding.
    for index in range(40):
        run_drip(now=now + timedelta(hours=5 * index), rng=random.Random(index))

    # Rolling, not calendar: check every 24h window that ends on an award.
    awards = list(DripAward.objects.filter(user=lucky))
    assert awards, "the run never picked the only candidate"
    for award in awards:
        window_total = sum(
            other.amount_minor
            for other in awards
            if award.awarded_at - timedelta(days=1) < other.awarded_at <= award.awarded_at
        )
        assert window_total <= DRIP_DAILY_CAP_PER_USER_MINOR


def test_a_run_respects_an_explicit_budget(db, players):
    fund_pool(biglie("100000"))

    summary = run_drip(rng=random.Random(2), budget_minor=30)

    assert summary["awarded_minor"] <= 30


def test_a_run_stops_when_the_pool_runs_out_mid_way(db, players):
    """The pool going dry is a stop condition, never a reason to retry smaller."""
    recognise_revenue(amount=biglie("1.20"), source="test")  # 10% = 12 scaglie
    pool = system_account(AccountKind.REDISTRIBUTION_POOL, VINTA).balance
    assert pool.minor == 12

    summary = run_drip(rng=random.Random(4), budget_minor=1_000)

    assert summary["awarded_minor"] <= pool.minor
    assert system_account(AccountKind.REDISTRIBUTION_POOL, VINTA).balance.minor >= 0


# ---------------------------------------------------------------------------
# Who is eligible
# ---------------------------------------------------------------------------


def test_a_self_excluded_user_is_never_dripped_on(db):
    """An unsolicited reward is solicitation. That is the thing they opted out of."""
    excluded = make_user("out@example.com", is_self_excluded=True)
    fund_pool()

    assert excluded not in list(eligible_users())

    run_drip(rng=random.Random(9))
    assert not DripAward.objects.filter(user=excluded).exists()


def test_an_underage_account_is_never_dripped_on(db):
    minor = make_user("teen@example.com", date_of_birth=date.today() - timedelta(days=365 * 15))
    fund_pool()

    run_drip(rng=random.Random(9))

    assert not DripAward.objects.filter(user=minor).exists()


def test_a_brand_new_account_waits(db):
    """Registration is free; without an age floor the drip pays whoever can
    create email addresses fastest."""
    fresh = make_user("fresh@example.com", joined_days_ago=0)
    fund_pool()

    assert fresh not in list(eligible_users())

    run_drip(rng=random.Random(9))
    assert not DripAward.objects.filter(user=fresh).exists()


def test_an_abandoned_account_stops_receiving(db):
    stale = make_user("ghost@example.com", joined_days_ago=400)
    fund_pool()

    assert stale not in list(eligible_users())


def test_an_inactive_account_is_excluded(db):
    dormant = make_user("closed@example.com", is_active=False)

    assert dormant not in list(eligible_users())


def test_the_cooldown_stops_two_runs_in_a_row_finding_the_same_person(db):
    only = make_user("solo@example.com")
    fund_pool()

    first = run_drip(rng=random.Random(1))
    assert first["recipients"] == 1

    second = run_drip(rng=random.Random(1))

    assert second["recipients"] == 0
    assert second["skipped"] == "no_eligible_users"
    assert DripAward.objects.filter(user=only).count() == 1


def test_eligibility_falls_back_to_the_join_date(db):
    """`last_seen_at` is only set once something touches it. A user nobody has
    tracked yet must still be a candidate rather than invisible forever."""
    user = make_user("untracked@example.com")
    get_user_model().objects.filter(pk=user.pk).update(last_seen_at=None)

    assert user in list(eligible_users())


# ---------------------------------------------------------------------------
# Randomness
# ---------------------------------------------------------------------------


def test_the_draw_is_random_rather_than_a_queue(db):
    """More candidates than slots, so the draw has to choose.

    Different seeds must produce different winners. If they do not, the "random"
    reward is a queue with extra steps, and the same accounts collect forever.
    """
    for index in range(12):
        make_user(f"r{index}@example.com")
    fund_pool(biglie("100000"))

    first = {a.user_id for a in _run_and_collect(random.Random(1), slots=3)}
    DripAward.objects.all().delete()
    second = {a.user_id for a in _run_and_collect(random.Random(99), slots=3)}

    assert len(first) == len(second) == 3
    assert first != second


def test_a_longer_wait_weighs_more_heavily(db):
    """Weighting spreads the reward instead of clustering it on the same names."""
    patient = make_user("patient@example.com", joined_days_ago=200)
    recent = make_user("recent@example.com", joined_days_ago=4)
    fund_pool()

    from economy.services import _selection_weight

    now = timezone.now()
    assert _selection_weight(patient, None, now) > _selection_weight(recent, None, now)


def _run_and_collect(rng, *, slots: int | None = None):
    run_drip(rng=rng, max_recipients=slots)
    return list(DripAward.objects.all())


# ---------------------------------------------------------------------------
# The scheduled entry point
# ---------------------------------------------------------------------------


def test_the_celery_task_runs_and_reports(db, players):
    """`config/celery.py` schedules this by name; it must be callable as-is."""
    fund_pool()

    summary = run_passive_drip()

    assert summary["recipients"] >= 1
    assert summary["asset"] == DRIP_ASSET


def test_the_task_is_a_no_op_on_an_empty_platform(db):
    summary = run_passive_drip()

    assert summary["recipients"] == 0
    assert summary["skipped"] in {"pool_empty", "no_eligible_users"}


def test_min_account_age_constant_is_a_real_fence(db):
    """Guards the constant itself: setting it to zero would silently reopen the
    farming path this test file exists to close."""
    assert DRIP_MIN_ACCOUNT_AGE >= timedelta(days=1)

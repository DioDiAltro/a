"""Sources against sinks, and the alarm that should go off when they diverge.

`docs/ECONOMY.md` is the specification these tests encode: only a confirmed euro
deposit mints biglie, and the sinks must out-weigh the sources or the currency
inflates. The monitoring module is what would notice — so it has to be tested
against economies that are deliberately broken, not just healthy ones.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.utils import timezone

from core.contracts import (
    CHARITY_BASIS_POINTS,
    REDISTRIBUTION_BASIS_POINTS,
    DepositConfirmed,
    GameSettled,
    RevenueRecognised,
    WithdrawalRequested,
)
from core.events import bus
from core.money import Amount, Asset, split_percentage
from economy.models import DripAward, EconomySnapshot, MintRecord, RevenueRecord
from economy.monitoring import (
    circulating_supply,
    inflation_alert,
    minted_total,
    sources_and_sinks,
    take_snapshot,
)
from economy.views import dashboard

VINTA = Asset.BIGLIA_VINTA
PIENA = Asset.BIGLIA_PIENA


def recognise(minor: int, *, source: str = "marketplace_fee") -> None:
    bus.publish_now(
        RevenueRecognised(
            amount_minor=minor, asset=VINTA, source=source, reference_id=uuid.uuid4()
        )
    )


def deposit(*, euro_minor: int, biglie_minor: int) -> None:
    bus.publish_now(
        DepositConfirmed(
            deposit_id=uuid.uuid4(), user_id=uuid.uuid4(),
            euro_minor=euro_minor, biglie_minor=biglie_minor,
        )
    )


def round_settled(*, stake: int, payout: int) -> None:
    bus.publish_now(
        GameSettled(
            round_id=uuid.uuid4(), user_id=uuid.uuid4(), game_key="slot",
            stake_minor=stake, payout_minor=payout, asset=VINTA,
            fairness_proof_id=uuid.uuid4(),
        )
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_revenue_is_recorded_with_its_carve_outs(db):
    """The carve-outs are recomputed here rather than read back from the ledger.

    Reading the same source twice cannot detect a disagreement; recomputing can.
    """
    recognise(10_000)

    record = RevenueRecord.objects.get()
    charity, _ = split_percentage(Amount(10_000, VINTA), CHARITY_BASIS_POINTS)
    redistribution, _ = split_percentage(Amount(10_000, VINTA), REDISTRIBUTION_BASIS_POINTS)

    assert record.amount_minor == 10_000
    assert record.charity_minor == charity.minor
    assert record.redistribution_minor == redistribution.minor
    assert record.source == "marketplace_fee"


def test_deposits_are_the_only_thing_counted_as_minting(db):
    deposit(euro_minor=5_000, biglie_minor=5_000)
    recognise(10_000)
    round_settled(stake=1_000, payout=900)

    assert MintRecord.objects.count() == 1
    assert minted_total() == Amount(5_000, PIENA)


def test_withdrawals_are_known_only_as_requests(db):
    """No completion event exists in the contract, so the figure is a bound."""
    bus.publish_now(
        WithdrawalRequested(
            withdrawal_id=uuid.uuid4(), user_id=uuid.uuid4(), euro_minor=2_500
        )
    )

    supply = circulating_supply()

    assert supply["withdrawal_requested"] == Amount(2_500, Asset.EUR)
    assert supply["is_exact"] is False


# ---------------------------------------------------------------------------
# Sources against sinks
# ---------------------------------------------------------------------------


def test_sources_and_sinks_separates_minting_from_movement(db):
    """The distinction the whole economy rests on: one of these three sources
    is new money and the other two are not."""
    deposit(euro_minor=10_000, biglie_minor=10_000)
    round_settled(stake=1_000, payout=1_500)  # house paid 500 net
    recognise(2_000)

    figures = sources_and_sinks()

    assert figures["sources"]["minted_minor"] == 10_000
    assert figures["sources"]["house_paid_minor"] == 500
    assert figures["sinks"]["revenue_minor"] == 2_000
    assert figures["net_to_users_minor"] == 10_000 + 500 - 2_000


def test_a_losing_round_does_not_count_as_a_source(db):
    round_settled(stake=1_000, payout=0)

    figures = sources_and_sinks()

    assert figures["sources"]["house_paid_minor"] == 0
    assert figures["games"]["realised_rtp_bp"] == 0


def test_events_outside_the_window_are_excluded(db):
    recognise(10_000)
    RevenueRecord.objects.update(occurred_at=timezone.now() - timedelta(days=30))

    assert sources_and_sinks(window=timedelta(days=7))["sinks"]["revenue_minor"] == 0


# ---------------------------------------------------------------------------
# The alarm
# ---------------------------------------------------------------------------


def test_a_healthy_economy_raises_nothing(db):
    deposit(euro_minor=100_000, biglie_minor=100_000)
    recognise(50_000)
    round_settled(stake=10_000, payout=9_400)

    assert inflation_alert() is None


def test_a_drip_that_outran_its_funding_is_an_alert(db):
    """The drip may only redistribute the share of revenue that feeds the pool.
    More than that means something is paying drips outside this context — and a
    drip that is not redistribution is issuance."""
    user = get_user_model().objects.create_user(
        email="d@example.com", password="x" * 12, date_of_birth=date(1990, 1, 1)
    )
    recognise(1_000)  # 10% = 100 into the pool
    DripAward.objects.create(
        run_id=uuid.uuid4(), user=user, amount_minor=5_000, asset=str(VINTA)
    )

    alert = inflation_alert()

    assert alert is not None
    assert alert["code"] == "drip_outran_funding"


def test_a_bleeding_bankroll_is_an_alert(db):
    """Payouts materially above revenue over a full window means an outcome
    table is wrong, and it compounds."""
    recognise(20_000)
    round_settled(stake=1_000, payout=60_000)

    alert = inflation_alert()

    assert alert is not None
    assert alert["code"] == "bankroll_bleed"


def test_minting_with_no_sinks_at_all_is_an_alert(db):
    """An economy of sources only inflates — the failure ECONOMY.md is written
    to prevent."""
    deposit(euro_minor=100_000, biglie_minor=100_000)

    alert = inflation_alert()

    assert alert is not None
    assert alert["code"] == "no_sinks"


def test_small_numbers_do_not_trigger_the_bankroll_alarm(db):
    """Below a floor the ratios are noise, and a watchdog that cries on noise
    gets muted, which is worse than not having one."""
    recognise(100)
    round_settled(stake=10, payout=500)

    assert inflation_alert() is None


# ---------------------------------------------------------------------------
# Snapshots and the dashboard
# ---------------------------------------------------------------------------


def test_a_snapshot_records_the_shape_of_the_economy(db):
    deposit(euro_minor=10_000, biglie_minor=10_000)
    recognise(5_000)

    snapshot = take_snapshot()

    assert snapshot.minted_minor == 10_000
    assert snapshot.revenue_minor == 5_000
    assert snapshot.net_to_users_minor == 5_000
    assert EconomySnapshot.objects.count() == 1


def test_the_snapshot_carries_the_alert_code(db):
    deposit(euro_minor=10_000, biglie_minor=10_000)

    assert take_snapshot().alert == "no_sinks"


def _request(user):
    request = RequestFactory().get("/economia/")
    request.user = user
    SessionMiddleware(lambda r: None).process_request(request)
    MessageMiddleware(lambda r: None).process_request(request)
    return request


def test_the_dashboard_renders_for_staff(db):
    staff = get_user_model().objects.create_user(
        email="staff@example.com", password="x" * 12,
        date_of_birth=date(1985, 1, 1), is_staff=True,
    )
    recognise(5_000)
    round_settled(stake=1_000, payout=940)

    response = dashboard(_request(staff))

    assert response.status_code == 200


def test_the_dashboard_is_closed_to_everyone_else(db):
    player = get_user_model().objects.create_user(
        email="curious@example.com", password="x" * 12, date_of_birth=date(1990, 1, 1)
    )

    response = dashboard(_request(player))

    assert response.status_code == 302  # bounced to login, not shown

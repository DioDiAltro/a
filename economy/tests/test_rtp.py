"""The RTP watchdog.

`docs/ECONOMY.md`: *"Uno scostamento significa che una tabella dei premi è
sbagliata, e va trattato come un incidente: le tabelle sono la promessa fatta al
giocatore."* So this file cares about two things — that rounds are actually
accumulated from the event (a watchdog nobody feeds is decoration), and that
drift is caught in both directions.

Paying too much is a bug that costs money. Paying too little is a bug that costs
trust, and only one of the two shows up in the revenue chart.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from core.contracts import DEFAULT_GAME_RTP_BASIS_POINTS, CreditClaimed, GameSettled
from core.events import bus
from core.money import Asset
from economy.constants import CLAIM_GAMBLE_KEY, RTP_MIN_ROUNDS_FOR_ALERT
from economy.models import GameRoundOutcome, RtpAudit
from economy.services import audit_rtp, configured_rtp_bp
from economy.tasks import audit_realised_rtp

VINTA = Asset.BIGLIA_VINTA


def settle(*, game_key="slot", stake=100, payout=50, user_id=None):
    """Publish a settled round the way `games` will."""
    bus.publish_now(
        GameSettled(
            round_id=uuid.uuid4(),
            user_id=user_id or uuid.uuid4(),
            game_key=game_key,
            stake_minor=stake,
            payout_minor=payout,
            asset=VINTA,
            fairness_proof_id=uuid.uuid4(),
        )
    )


def bulk_rounds(*, game_key: str, count: int, stake: int, payout: int, when=None) -> None:
    """Volume without the event overhead, for the alert-threshold tests."""
    when = when or timezone.now()
    GameRoundOutcome.objects.bulk_create(
        GameRoundOutcome(
            round_id=uuid.uuid4(),
            game_key=game_key,
            user_id=uuid.uuid4(),
            stake_minor=stake,
            payout_minor=payout,
            asset=str(VINTA),
            settled_at=when,
        )
        for _ in range(count)
    )


# ---------------------------------------------------------------------------
# Accumulation
# ---------------------------------------------------------------------------


def test_settled_rounds_are_accumulated_from_the_event(db):
    """`economy` never imports `games`; it only listens."""
    settle(game_key="wheel", stake=500, payout=1_000)

    outcome = GameRoundOutcome.objects.get()
    assert outcome.game_key == "wheel"
    assert outcome.stake_minor == 500
    assert outcome.payout_minor == 1_000
    assert outcome.margin_minor == -500  # the house lost this one
    assert outcome.origin == GameRoundOutcome.Origin.MINIGAME


def test_a_redelivered_round_is_counted_once(db):
    event = GameSettled(
        round_id=uuid.uuid4(), user_id=uuid.uuid4(), game_key="slot",
        stake_minor=100, payout_minor=0, asset=VINTA, fairness_proof_id=uuid.uuid4(),
    )
    bus.publish_now(event)
    bus.publish_now(event)

    assert GameRoundOutcome.objects.count() == 1


def test_the_claim_gamble_is_watched_like_any_other_game(db):
    """It runs on the same edge and it is the most-played surface on the site."""
    bus.publish_now(
        CreditClaimed(
            credit_id=uuid.uuid4(), user_id=uuid.uuid4(), settled_minor=0,
            original_minor=250, asset=VINTA, risked=True,
        )
    )

    outcome = GameRoundOutcome.objects.get()
    assert outcome.game_key == CLAIM_GAMBLE_KEY
    assert outcome.origin == GameRoundOutcome.Origin.CLAIM
    assert outcome.stake_minor == 250


def test_a_safe_claim_is_not_a_round(db):
    """It is a fee, not a wager. Counting it would drag measured RTP toward 99%
    and hide a genuinely broken game behind the average."""
    bus.publish_now(
        CreditClaimed(
            credit_id=uuid.uuid4(), user_id=uuid.uuid4(), settled_minor=99,
            original_minor=100, asset=VINTA, risked=False,
        )
    )

    assert not GameRoundOutcome.objects.exists()


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def test_realised_rtp_is_the_ratio_of_paid_to_staked(db):
    bulk_rounds(game_key="slot", count=RTP_MIN_ROUNDS_FOR_ALERT, stake=100, payout=94)

    audit = audit_rtp()[0]

    assert audit.rounds == RTP_MIN_ROUNDS_FOR_ALERT
    assert audit.realised_bp == 9_400
    assert audit.configured_bp == DEFAULT_GAME_RTP_BASIS_POINTS
    assert audit.drift_bp == 0
    assert not audit.is_alert


def test_a_game_paying_far_less_than_promised_raises_an_alert(db, caplog):
    """The direction that costs trust rather than money — and the one nobody
    would notice from the revenue chart."""
    bulk_rounds(game_key="slot", count=RTP_MIN_ROUNDS_FOR_ALERT, stake=100, payout=40)

    audit = audit_rtp()[0]

    assert audit.realised_bp == 4_000
    assert audit.drift_bp == 4_000 - DEFAULT_GAME_RTP_BASIS_POINTS
    assert audit.is_alert
    assert "RTP drift" in caplog.text


def test_a_game_paying_far_more_than_promised_also_raises_an_alert(db):
    bulk_rounds(game_key="wheel", count=RTP_MIN_ROUNDS_FOR_ALERT, stake=100, payout=150)

    audit = audit_rtp()[0]

    assert audit.realised_bp == 15_000
    assert audit.is_alert


def test_a_small_sample_is_recorded_but_never_alerts(db):
    """Under a couple of hundred rounds, realised RTP is noise. The row is still
    worth keeping — a history of 'fine, fine, fine, drift' is what makes the
    drift legible."""
    bulk_rounds(game_key="coinflip", count=5, stake=100, payout=0)

    audit = audit_rtp()[0]

    assert audit.realised_bp == 0
    assert not audit.is_alert
    assert "Campione troppo piccolo" in audit.note


def test_each_game_is_audited_separately(db):
    bulk_rounds(game_key="slot", count=RTP_MIN_ROUNDS_FOR_ALERT, stake=100, payout=94)
    bulk_rounds(game_key="wheel", count=RTP_MIN_ROUNDS_FOR_ALERT, stake=100, payout=20)

    audits = {a.game_key: a for a in audit_rtp()}

    assert set(audits) == {"slot", "wheel"}
    assert not audits["slot"].is_alert
    assert audits["wheel"].is_alert


def test_rounds_outside_the_window_are_ignored(db):
    bulk_rounds(
        game_key="slot", count=10, stake=100, payout=0,
        when=timezone.now() - timedelta(days=3),
    )

    assert audit_rtp(window=timedelta(hours=6)) == []


def test_the_reference_rtp_can_be_overridden_without_importing_games(db, settings):
    """`economy` may not depend on `games`, and `GameSettled` does not carry the
    configured figure, so the reference comes from settings."""
    settings.ECONOMY_CONFIGURED_RTP_BP = {"ladder": 8_000}

    assert configured_rtp_bp("ladder") == 8_000
    assert configured_rtp_bp("slot") == DEFAULT_GAME_RTP_BASIS_POINTS

    bulk_rounds(game_key="ladder", count=RTP_MIN_ROUNDS_FOR_ALERT, stake=100, payout=80)
    audit = audit_rtp()[0]

    assert audit.configured_bp == 8_000
    assert audit.drift_bp == 0


def test_a_game_with_no_stakes_is_skipped_rather_than_dividing_by_zero(db):
    bulk_rounds(game_key="freebie", count=3, stake=0, payout=0)

    assert audit_rtp() == []


# ---------------------------------------------------------------------------
# The scheduled entry point
# ---------------------------------------------------------------------------


def test_the_celery_task_runs_and_records(db):
    """`config/celery.py` schedules this by name, every six hours."""
    bulk_rounds(game_key="slot", count=RTP_MIN_ROUNDS_FOR_ALERT, stake=100, payout=94)

    result = audit_realised_rtp()

    assert result[0]["game_key"] == "slot"
    assert result[0]["realised_bp"] == 9_400
    assert RtpAudit.objects.count() == 1


@pytest.mark.parametrize("payout,expected_bp", [(0, 0), (100, 10_000), (33, 3_300)])
def test_realised_rtp_arithmetic(db, payout, expected_bp):
    bulk_rounds(game_key="slot", count=10, stake=100, payout=payout)

    assert audit_rtp()[0].realised_bp == expected_bp

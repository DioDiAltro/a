"""The ladder: the press-your-luck game, and the one with a decision in it.

Its design claim is stronger than "94% on average". It is that **every stopping
strategy returns exactly 94%** — cash out on the first rung or push to the top,
the expected value is identical. That falls out of making each step's survival
chance equal to `previous_rung / next_rung`, so the entire house edge is charged
once, on the first step, and climbing itself is EV-neutral.

That property is worth testing exactly rather than by simulation, because it is
the thing that makes the choice about nerve instead of about arithmetic the
player cannot do at the table.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from core.contracts import DEFAULT_GAME_RTP_BASIS_POINTS
from core.exceptions import IllegalStateTransition
from core.money import Amount
from games import services
from games.constants import GameKey, RoundState
from games.engine import LADDER_RUNGS, Outcome, get_game
from games.fairness import Rng
from games.tests.conftest import VINTA, available_minor
from wallet.constants import CreditState

LADDER = get_game(GameKey.LADDER)
STAKE = Amount(1_000, VINTA)


# ---------------------------------------------------------------------------
# Exact expected value
# ---------------------------------------------------------------------------


def test_every_stopping_point_is_worth_exactly_the_declared_rtp():
    """Computed from the odds, not sampled. The design claim, stated as a test.

    Reaching rung k has probability `prod(step_chance(1..k))` and pays
    `LADDER_RUNGS[k-1]`, so the product must be the declared RTP for every k.
    """
    reached = Fraction(1)

    for rung, value in enumerate(LADDER_RUNGS, start=1):
        reached *= LADDER.step_chance(rung)
        expected_value = reached * value

        assert expected_value == DEFAULT_GAME_RTP_BASIS_POINTS, (
            f"cashing out on rung {rung} is worth {float(expected_value):.2f}bp, "
            f"not {DEFAULT_GAME_RTP_BASIS_POINTS}"
        )


def test_each_step_is_riskier_than_the_one_before():
    """The promise the name makes. A ladder whose fourth rung is safer than its
    third is not a ladder, it is a lottery with extra clicks."""
    chances = [LADDER.step_chance(rung) for rung in range(1, len(LADDER_RUNGS) + 1)]
    assert chances == sorted(chances, reverse=True)
    assert all(0 < chance < 1 for chance in chances)


def test_the_rungs_pay_more_the_higher_they_are():
    assert list(LADDER_RUNGS) == sorted(LADDER_RUNGS)
    assert LADDER_RUNGS[0] > 10_000, "the first rung must beat simply getting the stake back"


def test_reaching_the_top_is_rare_but_possible():
    reached = Fraction(1)
    for rung in range(1, len(LADDER_RUNGS) + 1):
        reached *= LADDER.step_chance(rung)
    # 9400 / 250000: about one climb in twenty-seven.
    assert reached == Fraction(DEFAULT_GAME_RTP_BASIS_POINTS, LADDER_RUNGS[-1])


# ---------------------------------------------------------------------------
# Monte-Carlo — proves the derivation walks the odds it declares
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stop_at", [1, 3, 5, None])
def test_simulated_climbs_converge_on_the_declared_rtp(stop_at):
    """200k climbs per strategy. `stop_at=None` means never cash out.

    All four must land near 94%, which is the empirical face of the exactness
    proved above. The never-cash-out strategy needs the volume: its variance is
    dominated by a 25× top prize that lands once in twenty-seven climbs, so at
    50k rounds a two-sigma wobble is 400bp wide on its own.

    The stream is seeded deterministically, so this test either passes forever
    or fails forever — it cannot flake between runs.
    """
    rounds = 200_000
    staked = 0
    returned = 0

    for nonce in range(rounds):
        rng = Rng(server_seed="ladder-sim-" + "c" * 32, client_seed="s", nonce=nonce)
        detail = LADDER.derive(rng).detail
        path = detail["path"]

        climbed = 0
        for survived in path:
            if not survived:
                climbed = 0  # fell: everything is lost
                break
            climbed += 1
            if stop_at is not None and climbed >= stop_at:
                break

        staked += 10_000
        returned += LADDER.multiplier_at(climbed)

    realised = Fraction(returned, staked) * 10_000
    drift = abs(float(realised) - DEFAULT_GAME_RTP_BASIS_POINTS)

    assert drift < 400, (
        f"stopping at rung {stop_at} realised {float(realised):.0f}bp against a "
        f"declared {DEFAULT_GAME_RTP_BASIS_POINTS}bp"
    )


# ---------------------------------------------------------------------------
# The multi-step flow
# ---------------------------------------------------------------------------


@pytest.fixture
def path(monkeypatch):
    """Pin the whole climb, which is exactly what the real derivation does."""

    def _path(survived: list[bool]):
        fell_at = None if all(survived) else len(survived)
        outcome = Outcome(
            game_key=str(GameKey.LADDER),
            slot_key="cima" if fell_at is None else f"caduta:{fell_at}",
            label="forzato",
            multiplier_bp=LADDER_RUNGS[-1] if fell_at is None else 0,
            settled=False,
            detail={
                "path": survived,
                "fell_at": fell_at,
                "rungs": list(LADDER_RUNGS),
                "revealed_rungs": 0,
                "cashed_out": False,
                "final_rung": None,
            },
        )
        monkeypatch.setattr(LADDER, "derive", lambda rng: outcome)

    return _path


def _start(player, credit):
    return services.play_claim_gamble(
        user=player,
        credit_id=credit.pk,
        game_key=GameKey.LADDER,
        client_seed="x",
        idempotency_key=f"ladder:{credit.pk}",
    )


def test_a_ladder_round_stays_live_until_the_player_decides(db, player, pending_credit, path):
    path([True] * 8)
    credit = pending_credit(player, STAKE)

    game_round = _start(player, credit)

    assert game_round.state == RoundState.IN_PROGRESS
    credit.refresh_from_db()
    assert credit.state == CreditState.PENDING, "the credit settled before the climb ended"


def test_the_path_is_not_leaked_while_the_round_is_live(db, player, pending_credit, path):
    """The whole climb is decided at the first roll. Serving it to a live round
    would hand the player the answer."""
    path([True, True, False])
    credit = pending_credit(player, STAKE)
    game_round = _start(player, credit)

    payload = services.animation_payload(game_round)
    assert payload["outcome"]["path"] == []
    assert payload["outcome"]["fell_at"] is None

    services.ladder_step(user=player, round_id=game_round.pk)
    game_round.refresh_from_db()
    assert services.animation_payload(game_round)["outcome"]["path"] == [True]


def test_cashing_out_pays_the_current_rung(db, player, pending_credit, path):
    path([True, True, True, False])
    credit = pending_credit(player, STAKE)
    game_round = _start(player, credit)

    services.ladder_step(user=player, round_id=game_round.pk)
    services.ladder_step(user=player, round_id=game_round.pk)
    settled = services.ladder_cash_out(user=player, round_id=game_round.pk)

    assert settled.state == RoundState.SETTLED
    assert settled.multiplier_bp == LADDER_RUNGS[1]  # rung 2
    assert settled.payout_minor == STAKE.minor * LADDER_RUNGS[1] // 10_000
    assert available_minor(player) == settled.payout_minor

    credit.refresh_from_db()
    assert credit.state == CreditState.CLAIMED


def test_falling_loses_everything_and_burns_the_credit(db, player, pending_credit, path):
    path([True, False])
    credit = pending_credit(player, STAKE)
    game_round = _start(player, credit)

    services.ladder_step(user=player, round_id=game_round.pk)
    fallen = services.ladder_step(user=player, round_id=game_round.pk)

    assert fallen.state == RoundState.SETTLED
    assert fallen.payout_minor == 0
    credit.refresh_from_db()
    assert credit.state == CreditState.BURNED
    assert available_minor(player) == 0


def test_reaching_the_top_cashes_itself_out(db, player, pending_credit, path):
    """There is nothing left to risk, so leaving a button that cannot improve
    the player's position would only invite a misclick."""
    path([True] * len(LADDER_RUNGS))
    credit = pending_credit(player, STAKE)
    game_round = _start(player, credit)

    for _ in range(len(LADDER_RUNGS)):
        game_round = services.ladder_step(user=player, round_id=game_round.pk)

    assert game_round.state == RoundState.SETTLED
    assert game_round.multiplier_bp == LADDER_RUNGS[-1]
    assert game_round.payout_minor == STAKE.minor * LADDER_RUNGS[-1] // 10_000


def test_there_is_no_free_look(db, player, pending_credit, path):
    """Cashing out before climbing would be a risk-free claim that skips the 1%
    fee — a strictly better safe claim, reachable through the risky button."""
    path([True, True])
    credit = pending_credit(player, STAKE)
    game_round = _start(player, credit)

    with pytest.raises(IllegalStateTransition):
        services.ladder_cash_out(user=player, round_id=game_round.pk)


def test_a_settled_ladder_cannot_be_climbed_further(db, player, pending_credit, path):
    path([False])
    credit = pending_credit(player, STAKE)
    game_round = _start(player, credit)

    services.ladder_step(user=player, round_id=game_round.pk)

    with pytest.raises(IllegalStateTransition):
        services.ladder_step(user=player, round_id=game_round.pk)


def test_another_player_cannot_drive_someone_elses_climb(db, player, rival, pending_credit, path):
    path([True, True])
    credit = pending_credit(player, STAKE)
    game_round = _start(player, credit)

    with pytest.raises(IllegalStateTransition):
        services.ladder_step(user=rival, round_id=game_round.pk)


def test_a_finished_ladder_reveals_its_whole_path(db, player, pending_credit, path):
    """Once settled, hiding the rest would only obstruct verification."""
    path([True, False])
    credit = pending_credit(player, STAKE)
    game_round = _start(player, credit)

    services.ladder_step(user=player, round_id=game_round.pk)
    settled = services.ladder_step(user=player, round_id=game_round.pk)

    detail = services.animation_payload(settled)["outcome"]
    assert detail["path"] == [True, False]
    assert detail["fell_at"] == 2


def test_a_real_ladder_round_verifies_after_settlement(db, player, pending_credit):
    """No pinning: the real derivation, replayed from the revealed seed.

    The ladder is the case that would break a naive verifier, because the stored
    outcome gains `revealed_rungs` and `final_rung` as the player climbs and
    those are not products of the RNG.
    """
    credit = pending_credit(player, STAKE)
    game_round = _start(player, credit)

    services.ladder_step(user=player, round_id=game_round.pk)
    game_round.refresh_from_db()

    if game_round.state == RoundState.IN_PROGRESS:
        game_round = services.ladder_cash_out(user=player, round_id=game_round.pk)

    assert game_round.state == RoundState.SETTLED
    assert services.verify_round(game_round) is True

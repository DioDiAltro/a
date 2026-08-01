"""The claim gamble, end to end.

This is the flow the product is built around: a pending credit exists, the user
chooses *rischia* instead of *prendile*, and a minigame decides. What these
tests assert is not that the game is fun — it is that the money lands in exactly
one of two states, that a loss is recorded as a loss rather than as a
disappearance, and that a win is paid out of the house bankroll rather than
conjured.
"""

from __future__ import annotations

import pytest

from core.exceptions import BiglieError, IllegalStateTransition, SelfExcluded
from core.money import Amount
from games import services
from games.constants import GameKey, RoundState
from games.engine import get_game
from games.models import GameRound
from games.tests.conftest import VINTA, available_minor, house_minor, issuance_minor
from wallet.constants import CreditState

STAKE = Amount(1_000, VINTA)  # 10 biglie


# ---------------------------------------------------------------------------
# Losing
# ---------------------------------------------------------------------------


def test_losing_burns_the_credit_and_pays_nothing(db, player, pending_credit, force_outcome):
    """A lost gamble must be *burned*, not merely absent.

    `CreditState.BURNED` exists precisely so that "what did I actually receive"
    stays answerable in history. Marking a lost credit CLAIMED with a zero
    settlement would make the two indistinguishable a month later.
    """
    credit = pending_credit(player, STAKE)
    force_outcome(GameKey.COINFLIP, 0)

    game_round = services.play_claim_gamble(
        user=player,
        credit_id=credit.pk,
        game_key=GameKey.COINFLIP,
        client_seed="seme-di-prova",
        idempotency_key="t-lose",
    )

    credit.refresh_from_db()
    assert credit.state == CreditState.BURNED
    assert credit.settled_minor == 0
    assert credit.game_round_id == game_round.pk

    assert game_round.state == RoundState.SETTLED
    assert game_round.payout_minor == 0
    assert available_minor(player) == 0


def test_a_loss_does_not_destroy_supply(db, player, pending_credit, force_outcome):
    """The stake moves to the house; it is not burned out of circulation.

    If a lost stake reduced the issuance account, the platform's liability would
    silently shrink every time somebody lost, and `circulating_supply` would stop
    meaning what `docs/ECONOMY.md` says it means.
    """
    credit = pending_credit(player, STAKE)
    before_supply = issuance_minor()
    before_house = house_minor()

    force_outcome(GameKey.SLOT, 0)
    services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.SLOT,
        client_seed="x", idempotency_key="t-loss-supply",
    )

    assert issuance_minor() == before_supply
    assert house_minor() > before_house


# ---------------------------------------------------------------------------
# Winning
# ---------------------------------------------------------------------------


def test_winning_credits_the_multiplier(db, player, pending_credit, force_outcome):
    credit = pending_credit(player, STAKE)
    force_outcome(GameKey.WHEEL, 30_000)  # 3×

    game_round = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.WHEEL,
        client_seed="x", idempotency_key="t-win",
    )

    credit.refresh_from_db()
    assert credit.state == CreditState.CLAIMED
    assert game_round.payout_minor == 3_000
    assert credit.settled_minor == 3_000
    assert available_minor(player) == 3_000


def test_a_win_is_paid_from_the_house_not_minted(db, player, pending_credit, force_outcome):
    """The rule the whole economy rests on: only a euro deposit mints biglie.

    A 10× payout must move the house bankroll down, never move issuance. If this
    test fails the currency inflates every time somebody gets lucky, which is the
    failure `docs/ECONOMY.md` exists to prevent.
    """
    credit = pending_credit(player, STAKE)
    supply_before = issuance_minor()
    house_before = house_minor()

    force_outcome(GameKey.SLOT, 100_000)  # 10×
    services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.SLOT,
        client_seed="x", idempotency_key="t-jackpot",
    )

    assert issuance_minor() == supply_before, "a payout minted new biglie"
    # Stake in (+1000), payout out (-10000), so the bankroll drops by 9 000 —
    # and is allowed to, because it recovers by arithmetic over many rounds.
    assert house_minor() < house_before


def test_the_payout_rounds_down_never_up(db, player, pending_credit, force_outcome):
    """One scaglia a round, minted, is a real exploit at scale."""
    credit = pending_credit(player, Amount(333, VINTA))
    force_outcome(GameKey.COINFLIP, 18_800)  # 1.88× → 626.04 scaglie

    game_round = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.COINFLIP,
        client_seed="x", idempotency_key="t-round",
    )
    assert game_round.payout_minor == 626


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def test_a_self_excluded_user_cannot_gamble(db, player, pending_credit):
    credit = pending_credit(player, STAKE)
    player.is_self_excluded = True
    player.save(update_fields=["is_self_excluded"])

    with pytest.raises(SelfExcluded):
        services.play_claim_gamble(
            user=player, credit_id=credit.pk, game_key=GameKey.SLOT,
            client_seed="x", idempotency_key="t-excluded",
        )

    credit.refresh_from_db()
    assert credit.state == CreditState.PENDING, "an excluded user's credit was touched"


def test_a_credit_belonging_to_someone_else_does_not_resolve(db, player, rival, pending_credit):
    """Ownership is proven by the query, not checked afterwards."""
    credit = pending_credit(rival, STAKE)

    with pytest.raises(IllegalStateTransition):
        services.play_claim_gamble(
            user=player, credit_id=credit.pk, game_key=GameKey.SLOT,
            client_seed="x", idempotency_key="t-theft",
        )


def test_a_credit_cannot_be_gambled_twice(db, player, pending_credit, force_outcome):
    credit = pending_credit(player, STAKE)
    force_outcome(GameKey.COINFLIP, 0)

    services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.COINFLIP,
        client_seed="x", idempotency_key="t-first",
    )
    with pytest.raises(BiglieError):
        services.play_claim_gamble(
            user=player, credit_id=credit.pk, game_key=GameKey.COINFLIP,
            client_seed="x", idempotency_key="t-second",
        )


def test_replaying_the_same_key_returns_the_same_round(db, player, pending_credit, force_outcome):
    """A double-clicked button must not play twice."""
    credit = pending_credit(player, STAKE)
    force_outcome(GameKey.WHEEL, 20_000)

    first = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.WHEEL,
        client_seed="x", idempotency_key="t-idem",
    )
    second = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.WHEEL,
        client_seed="x", idempotency_key="t-idem",
    )

    assert first.pk == second.pk
    assert GameRound.objects.count() == 1


def test_the_duel_draw_cannot_be_chosen_for_a_claim(db, player, pending_credit):
    """It is 100% RTP by design and only reachable with two consenting stakes."""
    credit = pending_credit(player, STAKE)
    with pytest.raises(BiglieError):
        services.play_claim_gamble(
            user=player, credit_id=credit.pk, game_key=GameKey.DUEL,
            client_seed="x", idempotency_key="t-duelgame",
        )


# ---------------------------------------------------------------------------
# The proof
# ---------------------------------------------------------------------------


def test_a_settled_round_reveals_its_seed_and_verifies(db, player, pending_credit):
    """The user-facing guarantee, exercised through the real derivation."""
    credit = pending_credit(player, STAKE)

    game_round = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.WHEEL,
        client_seed="il-mio-seme", idempotency_key="t-verify",
    )

    proof = game_round.proof
    assert proof.is_revealed, "a settled round must publish its seed"
    assert proof.client_seed == "il-mio-seme"
    assert services.verify_round(game_round) is True


def test_a_tampered_seed_fails_verification(db, player, pending_credit):
    """Swapping the seed after the fact is the attack the scheme exists to stop."""
    credit = pending_credit(player, STAKE)
    game_round = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.SLOT,
        client_seed="onesto", idempotency_key="t-tamper",
    )

    proof = game_round.proof
    proof.server_seed = "f" * 64  # no longer hashes to the published commitment
    proof.save(update_fields=["server_seed"])

    game_round.refresh_from_db()
    assert services.verify_round(game_round) is False


def test_a_commitment_is_published_before_the_client_seed_exists(db, player):
    """The ordering that makes the commitment binding."""
    from games.fairness import commitment_for

    commitment = commitment_for(player)
    assert commitment.server_seed_hash
    assert commitment.client_seed == ""
    assert not commitment.is_spent

    # Asking again returns the same commitment, so the hash the player was shown
    # is the hash their next round actually uses.
    assert commitment_for(player).pk == commitment.pk


def test_a_commitment_is_spent_exactly_once(db, player, pending_credit):
    from core.exceptions import FairnessViolation
    from games.fairness import bind, commitment_for

    commitment = commitment_for(player)
    bind(commitment, client_seed="uno")

    with pytest.raises(FairnessViolation):
        bind(commitment, client_seed="due")


def test_the_animation_payload_carries_what_the_canvas_needs(db, player, pending_credit):
    """The frontend contract: wheels need wedges and an index, slots need reels."""
    credit = pending_credit(player, STAKE)
    game_round = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.WHEEL,
        client_seed="x", idempotency_key="t-payload",
    )

    payload = services.animation_payload(game_round)
    assert payload["game_key"] == "wheel"
    assert payload["verify_url"].endswith(f"{game_round.pk}/")

    wedges = payload["outcome"]["wedges"]
    assert 0 <= payload["outcome"]["wedge_index"] < len(wedges)
    assert set(wedges[0]) == {"label", "multiplier_bp", "weight"}

    # The winning wedge in the list must be the one the round actually paid.
    winner = wedges[payload["outcome"]["wedge_index"]]
    assert winner["multiplier_bp"] == game_round.multiplier_bp


def test_the_slot_payload_is_one_symbol_per_reel(db, player, pending_credit):
    credit = pending_credit(player, STAKE)
    game_round = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.SLOT,
        client_seed="x", idempotency_key="t-slot-payload",
    )

    reels = services.animation_payload(game_round)["outcome"]["reels"]
    assert len(reels) == 3
    assert all(isinstance(symbol, str) for symbol in reels)

    # The picture must agree with the payout: three alike is a win, always.
    definition = get_game(GameKey.SLOT)
    assert definition.key == "slot"
    if len(set(reels)) == 1:
        assert game_round.multiplier_bp > 0

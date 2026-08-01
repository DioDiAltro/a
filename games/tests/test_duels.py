"""Seller-versus-seller duels.

The mechanic is fun exactly to the extent that its edges are safe. Three things
have to hold or it becomes something else:

* **The pot is conserved.** Winner gets the pot minus the rake, to the scaglia.
* **Every non-resolving exit returns both stakes in full.** Declining or letting
  a challenge lapse must cost nothing, or "opt-in" is a word rather than a rule.
* **Consent and proportion are enforced before money moves.** Opting out is
  respected, the same person cannot be challenged all day, and nobody can stake
  past their level cap.
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from core.contracts import DUEL_RAKE_BASIS_POINTS
from core.exceptions import BiglieError, IllegalStateTransition, LimitExceeded
from core.money import Amount
from games import services
from games.constants import MAX_DUEL_CHALLENGES_PER_DAY, DuelState, GameKey
from games.engine import Outcome, get_game
from games.models import Duel
from games.tests.conftest import VINTA, available_minor, held_minor
from wallet.constants import CreditState
from wallet.models import PendingCredit

STAKE = Amount(1_000, VINTA)
PURSE = Amount(10_000, VINTA)


@pytest.fixture
def duellists(player, rival, seller, fund):
    seller(player)
    seller(rival)
    fund(player, PURSE)
    fund(rival, PURSE)
    return player, rival


@pytest.fixture
def challenger_wins(monkeypatch):
    """Pin the duel draw so a test can be about the money, not the dice."""

    def _pin(challenger_takes_it: bool):
        definition = get_game(GameKey.DUEL)
        outcome = Outcome(
            game_key=str(GameKey.DUEL),
            slot_key="sfidante" if challenger_takes_it else "sfidato",
            label="forzato",
            multiplier_bp=20_000 if challenger_takes_it else 0,
            detail={
                "rolls": [[900, 100]],
                "challenger_roll": 900,
                "defender_roll": 100,
                "ties": 0,
                "die": 1000,
                "challenger_wins": challenger_takes_it,
            },
        )
        monkeypatch.setattr(definition, "derive", lambda rng: outcome)

    return _pin


def _challenge(challenger, defender, stake=STAKE, key="d1"):
    return services.challenge(
        challenger=challenger,
        defender=defender,
        stake=stake,
        idempotency_key=f"test-duel:{key}",
    )


# ---------------------------------------------------------------------------
# Pot conservation
# ---------------------------------------------------------------------------


def test_the_winner_receives_the_pot_minus_the_rake_exactly(db, duellists, challenger_wins):
    """The invariant the loser will check first, and is entitled to."""
    challenger, defender = duellists
    challenger_wins(True)

    duel = _challenge(challenger, defender)
    services.accept_duel(
        duel=duel, defender=defender, client_seed="x", idempotency_key="a1"
    )

    duel.refresh_from_db()
    pot = STAKE.minor * 2
    expected_rake = pot * DUEL_RAKE_BASIS_POINTS // 10_000

    assert duel.state == DuelState.RESOLVED
    assert duel.winner_id == challenger.pk
    assert duel.rake_minor == expected_rake

    credit = PendingCredit.objects.get(user=challenger, reference_id=str(duel.pk))
    assert credit.amount_minor == pot - expected_rake
    assert credit.amount_minor + duel.rake_minor == pot, "the pot did not add up"
    assert credit.state == CreditState.PENDING, "winnings must go through the claim ritual"


def test_the_loser_pays_exactly_their_stake_and_no_more(db, duellists, challenger_wins):
    challenger, defender = duellists
    challenger_wins(True)

    before = available_minor(defender)
    duel = _challenge(challenger, defender)
    services.accept_duel(duel=duel, defender=defender, client_seed="x", idempotency_key="a2")

    assert available_minor(defender) == before - STAKE.minor
    assert held_minor(defender) == 0, "the loser's hold was not consumed"


def test_neither_side_keeps_a_hold_after_the_duel_resolves(db, duellists, challenger_wins):
    """A stranded hold is money the user can see and cannot spend."""
    challenger, defender = duellists
    challenger_wins(False)

    duel = _challenge(challenger, defender)
    services.accept_duel(duel=duel, defender=defender, client_seed="x", idempotency_key="a3")

    assert held_minor(challenger) == 0
    assert held_minor(defender) == 0


def test_the_defender_can_win(db, duellists, challenger_wins):
    challenger, defender = duellists
    challenger_wins(False)

    duel = _challenge(challenger, defender)
    services.accept_duel(duel=duel, defender=defender, client_seed="x", idempotency_key="a4")

    duel.refresh_from_db()
    assert duel.winner_id == defender.pk
    assert PendingCredit.objects.filter(user=defender, reference_id=str(duel.pk)).exists()
    # The round is booked from the challenger's seat, so a challenger who lost
    # shows a zero payout against their own stake.
    assert duel.round.payout_minor == 0


# ---------------------------------------------------------------------------
# Exits that must cost nothing
# ---------------------------------------------------------------------------


def test_challenging_holds_the_stake_immediately(db, duellists):
    """A challenge that costs nothing to send gets sent a hundred times."""
    challenger, defender = duellists
    before = available_minor(challenger)

    _challenge(challenger, defender)

    assert available_minor(challenger) == before - STAKE.minor
    assert held_minor(challenger) == STAKE.minor


def test_declining_returns_the_whole_stake(db, duellists):
    challenger, defender = duellists
    before = available_minor(challenger)

    duel = _challenge(challenger, defender)
    services.decline_duel(duel=duel, defender=defender)

    duel.refresh_from_db()
    assert duel.state == DuelState.DECLINED
    assert available_minor(challenger) == before, "declining cost the challenger money"
    assert held_minor(challenger) == 0


def test_expiring_returns_the_whole_stake(db, duellists):
    challenger, defender = duellists
    before = available_minor(challenger)

    duel = _challenge(challenger, defender)
    duel.expires_at = timezone.now() - timezone.timedelta(minutes=1)
    duel.save(update_fields=["expires_at"])

    from games.tasks import expire_stale_duels

    assert expire_stale_duels() == 1

    duel.refresh_from_db()
    assert duel.state == DuelState.EXPIRED
    assert available_minor(challenger) == before
    assert held_minor(challenger) == 0


def test_expiring_is_idempotent(db, duellists):
    """The sweeper runs on a schedule and must not care about losing a race."""
    challenger, defender = duellists
    duel = _challenge(challenger, defender)
    services.decline_duel(duel=duel, defender=defender)

    services.expire_duel(duel)  # no-op, already resolved
    duel.refresh_from_db()
    assert duel.state == DuelState.DECLINED


def test_an_expired_challenge_cannot_be_accepted(db, duellists):
    challenger, defender = duellists
    duel = _challenge(challenger, defender)
    duel.expires_at = timezone.now() - timezone.timedelta(seconds=1)
    duel.save(update_fields=["expires_at"])

    with pytest.raises(IllegalStateTransition):
        services.accept_duel(
            duel=duel, defender=defender, client_seed="x", idempotency_key="a5"
        )


def test_only_the_defender_may_answer(db, duellists):
    challenger, defender = duellists
    duel = _challenge(challenger, defender)

    with pytest.raises(IllegalStateTransition):
        services.decline_duel(duel=duel, defender=challenger)


# ---------------------------------------------------------------------------
# Consent and proportion
# ---------------------------------------------------------------------------


def test_opting_out_of_duels_is_respected(db, player, rival, seller, fund):
    """`SellerProfile.accepts_duels` is a refusal, not a preference."""
    seller(player)
    seller(rival, accepts_duels=False)
    fund(player, PURSE)

    with pytest.raises(LimitExceeded):
        _challenge(player, rival)

    assert not Duel.objects.exists()
    assert held_minor(player) == 0, "a refused challenge still took the stake"


def test_a_non_seller_cannot_start_a_duel(db, player, rival, seller, fund):
    """Duels are between sellers; that is the whole premise of the mechanic."""
    seller(rival)
    fund(player, PURSE)

    with pytest.raises(LimitExceeded):
        _challenge(player, rival)


def test_the_same_person_cannot_be_challenged_all_day(db, duellists):
    """Anti-harassment. Without it, opt-in only protects people who thought to
    opt out, and everybody else gets pestered until they do."""
    challenger, defender = duellists

    for index in range(MAX_DUEL_CHALLENGES_PER_DAY):
        _challenge(challenger, defender, key=f"h{index}")

    with pytest.raises(LimitExceeded) as caught:
        _challenge(challenger, defender, key="one-too-many")

    assert caught.value.limit_name == "duel:harassment"
    assert Duel.objects.count() == MAX_DUEL_CHALLENGES_PER_DAY


def test_the_cap_is_per_defender_not_global(db, player, rival, seller, fund):
    """Challenging four different people is sociable; challenging one person
    four times is not."""
    from games.tests.conftest import _make_user

    seller(player)
    fund(player, PURSE)

    for index in range(MAX_DUEL_CHALLENGES_PER_DAY + 1):
        target = _make_user(f"target{index}@example.com")
        seller(target)
        _challenge(player, target, key=f"spread{index}")

    assert Duel.objects.count() == MAX_DUEL_CHALLENGES_PER_DAY + 1


def test_a_stake_above_the_level_cap_is_refused(db, player, rival, seller, fund):
    """The cap is the main protection against a whale draining a new seller."""
    seller(player, xp=0)  # level 1: may_duel is False
    seller(rival)
    fund(player, Amount(1_000_000, VINTA))

    with pytest.raises(LimitExceeded) as caught:
        _challenge(player, rival, stake=Amount(500_000, VINTA))

    assert caught.value.limit_name == "duel:level_cap"
    assert held_minor(player) == 0


def test_a_non_wagerable_asset_cannot_be_staked(db, duellists):
    challenger, defender = duellists
    from core.money import Asset

    with pytest.raises(BiglieError):
        _challenge(challenger, defender, stake=Amount(1_000, Asset.EUR))


def test_nobody_can_duel_themselves(db, player, seller, fund):
    seller(player)
    fund(player, PURSE)

    with pytest.raises(BiglieError):
        _challenge(player, player)


def test_a_self_excluded_challenger_is_stopped(db, duellists):
    from core.exceptions import SelfExcluded

    challenger, defender = duellists
    challenger.is_self_excluded = True
    challenger.save(update_fields=["is_self_excluded"])

    with pytest.raises(SelfExcluded):
        _challenge(challenger, defender)


def test_self_exclusion_releases_an_open_duel(db, duellists):
    """`SelfExclusionStarted` asks handlers to refund staked wagers, not merely
    block login. An open duel is a staked wager and an open invitation."""
    challenger, defender = duellists
    before = available_minor(challenger)
    _challenge(challenger, defender)

    services.cancel_open_duels_for(challenger)

    assert available_minor(challenger) == before
    assert held_minor(challenger) == 0


# ---------------------------------------------------------------------------
# Fairness
# ---------------------------------------------------------------------------


def test_the_commitment_is_published_before_the_defender_answers(db, duellists):
    """The challenger cannot know the defender's client seed; the defender
    cannot see a server seed committed before they were asked."""
    challenger, defender = duellists
    duel = _challenge(challenger, defender)

    assert duel.proof is not None
    assert duel.proof.server_seed_hash
    assert duel.proof.client_seed == "", "the seed was bound before the defender chose"


def test_a_resolved_duel_verifies(db, duellists):
    challenger, defender = duellists
    duel = _challenge(challenger, defender)
    services.accept_duel(
        duel=duel, defender=defender, client_seed="scelto-da-me", idempotency_key="a9"
    )

    duel.refresh_from_db()
    assert duel.proof.is_revealed
    assert duel.proof.client_seed == "scelto-da-me"
    assert services.verify_round(duel.round) is True


def test_accepting_twice_settles_once(db, duellists, challenger_wins):
    challenger, defender = duellists
    challenger_wins(True)

    duel = _challenge(challenger, defender)
    services.accept_duel(duel=duel, defender=defender, client_seed="x", idempotency_key="same")

    with pytest.raises(IllegalStateTransition):
        services.accept_duel(
            duel=duel, defender=defender, client_seed="x", idempotency_key="same"
        )

    assert PendingCredit.objects.filter(reference_id=str(duel.pk)).count() == 1

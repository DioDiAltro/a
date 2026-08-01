"""The two properties a money game cannot be shipped without.

**The RNG must be unbiased.** The naive way to get a number in [0, n) from random
bytes is `byte % n`, and when n does not divide 256 the low values come up more
often. On one round nobody notices; over a million rounds it is an undeclared
edge, which is a different word for fraud.

**Every table must pay what it claims.** The declared RTP is a promise. These
tests compute the expected value exactly from the weights — no simulation, no
tolerance — and then separately simulate to prove the derivation actually walks
the table it declares.
"""

from __future__ import annotations

from collections import Counter
from fractions import Fraction

import pytest

from core.contracts import DEFAULT_GAME_RTP_BASIS_POINTS
from core.exceptions import FairnessViolation, InvalidAmount
from games.engine import ONE_X_BP, expected_rtp, playable_games
from games.fairness import Rng, hash_seed, new_server_seed


def make_rng(nonce: int = 0) -> Rng:
    return Rng(server_seed="a" * 64, client_seed="player-seed", nonce=nonce)


# ---------------------------------------------------------------------------
# The byte stream
# ---------------------------------------------------------------------------


def test_the_stream_is_deterministic():
    """Replay is the whole basis of verification: same inputs, same bytes."""
    assert make_rng(7).next_bytes(64) == make_rng(7).next_bytes(64)


def test_different_nonces_give_different_streams():
    assert make_rng(1).next_bytes(32) != make_rng(2).next_bytes(32)


def test_successive_draws_advance_the_cursor():
    rng = make_rng()
    assert rng.next_bytes(16) != rng.next_bytes(16)


def test_the_stream_crosses_block_boundaries_cleanly():
    """One 200-byte draw must equal five sequential draws off the same stream.

    HMAC-SHA256 gives 32 bytes at a time, so anything longer spans blocks. A
    buffering bug here would silently repeat or skip bytes.
    """
    single = make_rng().next_bytes(200)

    rng = make_rng()
    pieces = b"".join(rng.next_bytes(40) for _ in range(5))

    assert single == pieces


# ---------------------------------------------------------------------------
# Uniformity — the modulo bias test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [3, 6, 7, 10, 37, 100])
def test_random_below_is_uniform(n):
    """Chi-square goodness of fit against a flat distribution.

    n values are chosen to not divide any power of two, which is exactly the
    case where `% n` skews. A modulo implementation fails this comfortably.
    """
    draws = 60_000
    rng = make_rng(nonce=99)
    counts = Counter(rng.random_below(n) for _ in range(draws))

    assert set(counts) <= set(range(n)), "produced a value outside the range"

    expected = draws / n
    chi_square = sum((counts.get(v, 0) - expected) ** 2 / expected for v in range(n))

    # Critical value at p=0.001 for n-1 degrees of freedom, generously rounded
    # up. Loose enough not to flake, tight enough that modulo bias trips it.
    critical = {3: 13.8, 6: 20.5, 7: 22.5, 10: 27.9, 37: 69.3, 100: 149.4}[n]
    assert chi_square < critical, f"distribution for n={n} is skewed (X²={chi_square:.1f})"


def test_random_below_covers_its_whole_range():
    rng = make_rng(nonce=5)
    seen = {rng.random_below(12) for _ in range(2_000)}
    assert seen == set(range(12))


def test_random_below_rejects_a_non_positive_bound():
    with pytest.raises(InvalidAmount):
        make_rng().random_below(0)


# ---------------------------------------------------------------------------
# Commit-reveal
# ---------------------------------------------------------------------------


def test_the_published_hash_matches_the_revealed_seed():
    """What a suspicious player checks by hand after a losing round."""
    seed = new_server_seed()
    published = hash_seed(seed)

    assert hash_seed(seed) == published
    assert hash_seed(new_server_seed()) != published


def test_a_substituted_seed_fails_its_commitment():
    """The attack the scheme exists to stop: swapping the seed after seeing the
    client's, to pick a losing outcome."""
    honest = new_server_seed()
    published = hash_seed(honest)

    substituted = new_server_seed()
    assert hash_seed(substituted) != published


def test_an_empty_server_seed_is_refused():
    with pytest.raises(FairnessViolation):
        Rng(server_seed="", client_seed="x", nonce=0)


# ---------------------------------------------------------------------------
# RTP — the promise
# ---------------------------------------------------------------------------


def test_every_game_table_pays_exactly_what_it_declares():
    """Exact rational arithmetic over the weights. No tolerance, no sampling.

    `GameDefinition.__init__` already asserts this, so a bad table cannot even
    be constructed — this test states the property in the open so that removing
    the assertion breaks something visible.
    """
    for game in playable_games():
        # `expected_rtp` reports in basis points, the same unit the game
        # declares, so the comparison needs no conversion and no epsilon.
        actual = expected_rtp(game.table)

        assert actual == Fraction(game.rtp_bp), (
            f"{game.key} declares {game.rtp_bp}bp but its table pays "
            f"{float(actual):.1f}bp"
        )


def test_the_house_edge_is_positive_on_every_game():
    """An RTP at or above 100% is a game the platform pays to run."""
    for game in playable_games():
        assert 0 < game.house_edge_bp < 2_000, f"{game.key} edge is {game.house_edge_bp}bp"


def test_games_default_to_the_declared_platform_rtp():
    for game in playable_games():
        assert game.rtp_bp == DEFAULT_GAME_RTP_BASIS_POINTS or game.rtp_bp > 8_000


def test_hit_rate_is_reported_alongside_rtp():
    """A 94% game paying once in fifty feels nothing like one paying every other
    round. Both numbers have to be available to show the player."""
    for game in playable_games():
        assert 0 < game.hit_rate <= 1


def test_paytable_weights_sum_to_the_stated_chances():
    for game in playable_games():
        total = sum(row["chance_percent"] for row in game.paytable())
        assert abs(total - 100) < 0.01, f"{game.key} odds sum to {total}%"


# ---------------------------------------------------------------------------
# Simulation — proves the derivation walks the table it declares
# ---------------------------------------------------------------------------


def test_simulated_play_converges_on_the_declared_rtp():
    """The exact test above proves the *table* is right. This proves `derive`
    actually samples that table, rather than some other distribution."""
    rounds = 40_000

    for game in playable_games():
        if game.multi_step:
            continue  # the ladder settles over several steps; covered separately

        staked = 0
        returned = 0
        for nonce in range(rounds):
            rng = Rng(server_seed="sim-seed-" + "b" * 40, client_seed="c", nonce=nonce)
            outcome = game.derive(rng)
            staked += ONE_X_BP
            returned += outcome.multiplier_bp

        realised = Fraction(returned, staked)
        declared = Fraction(game.rtp_bp, ONE_X_BP)
        drift = abs(float(realised - declared))

        # 3% absolute tolerance: wide enough that a high-variance table does not
        # flake at 40k rounds, narrow enough to catch a derivation reading the
        # wrong column.
        assert drift < 0.03, (
            f"{game.key} simulated {float(realised) * 100:.2f}% against a "
            f"declared {float(declared) * 100:.2f}%"
        )

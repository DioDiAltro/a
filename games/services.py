"""The games context's public API.

Everything that moves money because of a game goes through this module, and
nothing else in `games` is callable from outside it.

The shape of the JSON the frontend consumes
===========================================
`animation_payload(round)` returns this, and the canvas modules in
`web/static/js/games/` are written against it::

    {
      "round_id":      "0f0c…",          # uuid, also the verify URL segment
      "game_key":      "wheel",
      "state":         "settled",        # committed | in_progress | settled | cancelled
      "asset":         "BIGLIA_VINTA",
      "stake_minor":   500,              # integer scaglie, never a float
      "payout_minor":  750,              # null while a ladder is still climbing
      "multiplier_bp": 15000,            # 10000 = stake returned
      "won":           true,
      "label":         "1.5×",           # short, already in Italian
      "verify_url":    "/games/verifica/0f0c…/",
      "fairness":      { "server_seed_hash": …, "client_seed": …, "nonce": 3,
                         "server_seed": null,   # until revealed
                         "revealed": false, "message": "seed:3:0" },
      "outcome":       { …game-specific, see below… }
    }

Per game, ``outcome`` carries exactly what its animation needs and nothing the
player should not yet know:

``slot``
    ``{"reels": ["◍", "◍", "7"], "symbol_names": ["biglia", "biglia", "sette"]}``
    — one symbol per reel, in reel order. `slot.js` plants these on the payline
    and spins to them.

``wheel``
    ``{"wedge_index": 17, "wedges": [{"label": "1.5×", "multiplier_bp": 15000,
    "weight": 1}, …]}`` — `wheel.js` sizes each arc by ``weight`` and spins to
    ``wedge_index``. Every wedge is a separate entry with equal weight, so the
    arcs the player sees *are* the odds.

``coinflip``
    ``{"face": "testa", "won": true, "flips": 11}`` — ``flips`` is the number of
    half-rotations to animate, drawn from the same verifiable stream.

``ladder``
    ``{"rungs": [12000, …], "path": [true, true], "revealed_rungs": 2,
    "fell_at": null, "cashed_out": false, "final_rung": null}`` — while the round
    is live, ``path`` is truncated to what the player has actually climbed and
    ``fell_at`` is withheld. Both are filled in once it settles, because at that
    point hiding them would only obstruct verification.

``duel``
    ``{"rolls": [[412, 412], [871, 233]], "challenger_roll": 871,
    "defender_roll": 233, "ties": 1, "die": 1000, "challenger_wins": true}``.

Why the outcome is decided before any pixel moves
=================================================
Each of these functions derives the result inside a database transaction and
commits it, then hands the client something to animate toward. A player who
closes the tab mid-spin, replays the request or edits the JavaScript changes
nothing: the round is already settled and the ledger already moved. That is the
contract `realtime.consumers` is written against — the socket relays, it never
decides.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from core.contracts import DuelChallenged, DuelResolved, GameSettled
from core.events import bus
from core.exceptions import IllegalStateTransition, InvalidAmount
from core.idempotency import idempotent
from core.money import Amount
from games import fairness, policy
from games.constants import DUEL_EXPIRY, DuelState, GameKey, RoundSource, RoundState
from games.engine import ONE_X_BP, get_game, payout_minor, playable_games
from games.models import Duel, GameRound
from wallet.constants import AccountKind, CreditSource, EntryKind
from wallet.selectors import get_claimable_credit
from wallet.services import (
    capture_hold,
    credit_pending,
    place_hold,
    recognise_revenue,
    release_hold,
    settle_risked_claim,
)

logger = logging.getLogger(__name__)

__all__ = [
    "accept_duel",
    "animation_payload",
    "available_games",
    "challenge",
    "decline_duel",
    "expire_duel",
    "history_for",
    "ladder_cash_out",
    "ladder_step",
    "play_claim_gamble",
    "verify_round",
]


@dataclass(frozen=True, slots=True)
class _HoldRef:
    """The minimum `wallet.services` needs to resolve a hold we stored by id.

    `release_hold` and `capture_hold` both re-read the row under
    ``select_for_update()`` and touch nothing but ``pk`` on the object handed to
    them, so a games row holding a bare UUID can point at a hold without
    importing `wallet.models` — which it must not do.

    This exists because the wallet's public API can *create* a hold and consume
    one it is given, but offers no ``get_hold(id)``. The moment
    `wallet.selectors` grows one, this class and every use of it should go.
    """

    pk: uuid.UUID


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def available_games() -> list:
    """The minigames a player may pick for a claim gamble."""
    return playable_games()


def history_for(user, *, limit: int = 50) -> list[GameRound]:
    return list(GameRound.objects.history(user)[:limit])


def get_round(user, round_id) -> GameRound | None:
    return GameRound.objects.filter(pk=round_id, user=user).select_related("proof").first()


def get_round_for_verification(round_id) -> GameRound | None:
    """Any round, by id, without an ownership check — on purpose.

    Verification pages are public. A result that only the person who lost it can
    inspect is not much of a fairness guarantee: being able to hand somebody
    else the link and have them check it independently is the point.
    """
    return GameRound.objects.filter(pk=round_id).select_related("proof").first()


def verify_round(game_round: GameRound) -> bool:
    """Re-derive a settled round from its revealed seed. The user-facing check."""
    return fairness.verify(game_round.proof, game_round.outcome)


def animation_payload(game_round: GameRound) -> dict[str, Any]:
    """Everything the canvas needs. See the module docstring for the shape."""
    definition = get_game(game_round.game_key)
    return {
        "round_id": str(game_round.pk),
        "game_key": game_round.game_key,
        "game_name": definition.name,
        "state": game_round.state,
        "asset": game_round.asset,
        "stake_minor": game_round.stake_minor,
        "payout_minor": game_round.payout_minor,
        "multiplier_bp": game_round.multiplier_bp,
        "won": game_round.is_win,
        "label": game_round.outcome.get("label", ""),
        "verify_url": reverse("games:verify", kwargs={"round_id": game_round.pk}),
        "fairness": game_round.proof.public_dict(),
        "outcome": game_round.public_detail(),
    }


# ---------------------------------------------------------------------------
# The claim gamble — the flow the whole product hangs on
# ---------------------------------------------------------------------------


@transaction.atomic
def play_claim_gamble(
    *,
    user,
    credit_id,
    game_key: str,
    client_seed: str = "",
    idempotency_key: str,
) -> GameRound:
    """The user chose *rischia* on a pending credit. Play it.

    `user` is part of the signature rather than looked up from the credit
    because ownership must be proven by the query, not checked after the fact:
    `wallet.selectors.get_claimable_credit` scopes by user, so a credit id
    belonging to somebody else simply does not resolve.

    One-shot games settle before this returns. The ladder does not — it comes
    back `IN_PROGRESS` and the player drives it with `ladder_step` and
    `ladder_cash_out`.
    """
    # The replay check comes first, before the credit is even looked at. By the
    # time a double-clicked button arrives the credit is already CLAIMED or
    # BURNED, so resolving it first would turn a harmless retry into "questo
    # riscatto non è più disponibile" — the one error message guaranteed to
    # alarm somebody who just watched their money move.
    with idempotent(
        idempotency_key,
        scope="games.claim_gamble",
        payload={"credit": str(credit_id), "game": str(game_key)},
    ) as handle:
        if handle.replayed:
            return GameRound.objects.select_related("proof").get(pk=handle.result_ref)

        definition = get_game(game_key)
        if definition.key not in {g.key for g in playable_games()}:
            raise InvalidAmount("Questo gioco non si può scegliere per un riscatto.")

        credit = get_claimable_credit(user, credit_id)
        if credit is None or not credit.is_claimable:
            raise IllegalStateTransition(
                current="unavailable",
                attempted="gamble",
                message="Questo riscatto non è più disponibile.",
            )

        stake = credit.amount
        policy.assert_may_wager(user, stake)

        game_round, outcome = _open_round(
            user=user,
            definition=definition,
            stake=stake,
            client_seed=client_seed,
            source=RoundSource.CLAIM_GAMBLE,
            credit_id=credit.pk,
        )

        if definition.multi_step:
            # The stake is committed the moment the round opens even though the
            # credit is settled later: the player is already at risk, so the
            # wager limit must see it now rather than when they stop climbing.
            game_round.state = RoundState.IN_PROGRESS
            game_round.save(update_fields=["state", "updated_at"])
        else:
            _settle_claim_round(game_round, credit, outcome.multiplier_bp)

        handle.result_ref = str(game_round.pk)
        return game_round


def _open_round(
    *,
    user,
    definition,
    stake: Amount,
    client_seed: str,
    source: str,
    credit_id=None,
    proof=None,
) -> tuple[GameRound, Any]:
    """Bind a commitment, derive the outcome and write the round.

    The row is written before any money moves so that a crash mid-settlement
    leaves an auditable `COMMITTED` round rather than nothing at all.
    """
    if proof is None:
        proof = fairness.commitment_for(user, purpose=str(definition.key))
    fairness.bind(proof, client_seed=client_seed)

    outcome = definition.derive(proof.rng())

    game_round = GameRound.objects.create(
        user=user,
        game_key=definition.key,
        source=source,
        state=RoundState.COMMITTED,
        stake_minor=stake.minor,
        asset=stake.asset,
        proof=proof,
        outcome=outcome.as_json(),
        credit_id=credit_id,
    )
    policy.record_wager(user, stake, reference=f"round:{game_round.pk}")
    return game_round, outcome


def _settle_claim_round(game_round: GameRound, credit, multiplier_bp: int) -> GameRound:
    """Hand the verdict to the ledger and close the round.

    `wallet.services.settle_risked_claim` is the only thing that touches money
    here. It pays out of the house bankroll — never from issuance — so a round
    that goes badly for the platform is the house's own accumulated edge
    changing hands rather than new currency.
    """
    stake = game_round.stake
    payout = Amount(payout_minor(stake.minor, multiplier_bp), stake.asset)

    settled = settle_risked_claim(
        credit=credit,
        payout=payout,
        game_round_id=game_round.pk,
        idempotency_key=f"game-settle:{game_round.pk}",
    )

    game_round.payout_minor = settled.minor
    game_round.multiplier_bp = multiplier_bp
    game_round.state = RoundState.SETTLED
    game_round.settled_at = timezone.now()
    game_round.save(
        update_fields=["payout_minor", "multiplier_bp", "state", "settled_at", "updated_at"]
    )

    fairness.reveal(game_round.proof)
    policy.record_loss(game_round.user, stake, settled, reference=f"round:{game_round.pk}")

    bus.publish(
        GameSettled(
            round_id=game_round.id,
            user_id=game_round.user_id,
            game_key=str(game_round.game_key),
            stake_minor=stake.minor,
            payout_minor=settled.minor,
            asset=stake.asset,
            fairness_proof_id=game_round.proof_id,
        )
    )
    return game_round


# ---------------------------------------------------------------------------
# The ladder — the one game with server-side state between requests
# ---------------------------------------------------------------------------


def _live_ladder(user, round_id) -> GameRound:
    game_round = GameRound.objects.select_for_update().filter(
        pk=round_id, user=user, game_key=GameKey.LADDER
    ).first()

    if game_round is None:
        raise IllegalStateTransition(
            current="missing", attempted="ladder", message="Partita non trovata."
        )
    if game_round.state != RoundState.IN_PROGRESS:
        raise IllegalStateTransition(
            current=game_round.state,
            attempted="ladder",
            message="Questa scalata è già finita.",
        )
    return game_round


@transaction.atomic
def ladder_step(*, user, round_id) -> GameRound:
    """Climb one rung.

    Nothing is *decided* here — the whole path was rolled at round creation and
    committed to the proof. This reveals the next step of it. That ordering is
    what stops the server from reacting to a player who is doing well, and it
    means the entire climb is covered by one commit-reveal the player can check
    in a single operation.
    """
    game_round = _live_ladder(user, round_id)
    definition = get_game(GameKey.LADDER)

    detail = dict(game_round.outcome.get("detail") or {})
    path: list[bool] = list(detail.get("path") or [])
    revealed = int(detail.get("revealed_rungs") or 0)

    if revealed >= len(path):
        raise IllegalStateTransition(
            current="exhausted", attempted="step", message="Non ci sono altri gradini."
        )

    survived = bool(path[revealed])
    revealed += 1
    detail["revealed_rungs"] = revealed
    _write_detail(game_round, detail)

    if not survived:
        return _settle_ladder(game_round, rung=0, cashed_out=False)

    if revealed >= len(definition.rungs):
        # Reached the top: there is nothing left to risk, so it cashes itself
        # out rather than leaving the player staring at a button that cannot
        # improve their position.
        return _settle_ladder(game_round, rung=revealed, cashed_out=True)

    return game_round


@transaction.atomic
def ladder_cash_out(*, user, round_id) -> GameRound:
    """Stop climbing and take what the current rung is worth."""
    game_round = _live_ladder(user, round_id)

    detail = dict(game_round.outcome.get("detail") or {})
    revealed = int(detail.get("revealed_rungs") or 0)

    if revealed <= 0:
        raise IllegalStateTransition(
            current="rung_zero",
            attempted="cash_out",
            message="Devi salire almeno un gradino prima di incassare.",
        )

    return _settle_ladder(game_round, rung=revealed, cashed_out=True)


def _write_detail(game_round: GameRound, detail: dict) -> None:
    outcome = dict(game_round.outcome)
    outcome["detail"] = detail
    game_round.outcome = outcome
    game_round.save(update_fields=["outcome", "updated_at"])


def _settle_ladder(game_round: GameRound, *, rung: int, cashed_out: bool) -> GameRound:
    definition = get_game(GameKey.LADDER)
    multiplier = definition.multiplier_at(rung)

    detail = dict(game_round.outcome.get("detail") or {})
    detail["cashed_out"] = cashed_out
    detail["final_rung"] = rung if rung else None

    # `outcome` stays the record of what the *RNG* produced — the whole climb,
    # as derived. What the player chose to take is `GameRound.multiplier_bp` and
    # `payout_minor`. Overwriting the derived multiplier here would make the
    # stored outcome disagree with its own replay, and verification would fail
    # on every honest ladder round that cashed out early.
    outcome = dict(game_round.outcome)
    outcome["detail"] = detail
    outcome["settled"] = True
    outcome["label"] = (
        f"Incassato al gradino {rung}" if multiplier else "Caduto: niente"
    )
    game_round.outcome = outcome

    credit = get_claimable_credit(game_round.user, game_round.credit_id)
    if credit is None or not credit.is_claimable:
        # The credit was claimed safely from another tab, or expired, while the
        # player was still climbing. There is nothing left to settle against.
        # The round is cancelled and *returned* rather than raised, because
        # raising inside this transaction would roll the cancellation back and
        # leave a round that looks live but can never finish.
        game_round.state = RoundState.CANCELLED
        game_round.outcome = outcome | {"label": "Annullata: riscatto non più disponibile"}
        game_round.save(update_fields=["outcome", "state", "updated_at"])
        fairness.reveal(game_round.proof)
        logger.warning("Ladder round %s lost its credit before settling.", game_round.pk)
        return game_round

    game_round.save(update_fields=["outcome", "updated_at"])
    return _settle_claim_round(game_round, credit, multiplier)


# ---------------------------------------------------------------------------
# Duels — sellers playing each other for their biglie
# ---------------------------------------------------------------------------


@transaction.atomic
def challenge(
    *,
    challenger,
    defender,
    stake: Amount,
    idempotency_key: str,
    message: str = "",
) -> Duel:
    """Stake biglie against another seller and ask them to match it.

    The challenger's stake goes into a hold immediately — a challenge that costs
    nothing to send is a challenge that gets sent a hundred times — and the
    fairness commitment is published *now*, before the defender has supplied a
    client seed. Neither side can steer the result: the challenger does not know
    the seed the defender will pick, and the defender cannot see the server seed
    that was committed before they were asked.
    """
    policy.assert_may_challenge(challenger, defender, stake)

    with idempotent(
        idempotency_key,
        scope="games.duel_challenge",
        payload={"defender": str(defender.pk), "amt": stake.minor},
    ) as handle:
        if handle.replayed:
            return Duel.objects.get(pk=handle.result_ref)

        proof = fairness.commit(user=challenger, purpose="duel")
        duel = Duel.objects.create(
            challenger=challenger,
            defender=defender,
            stake_minor=stake.minor,
            asset=stake.asset,
            expires_at=timezone.now() + DUEL_EXPIRY,
            proof=proof,
            message=message[:140],
        )

        hold = place_hold(
            user=challenger,
            amount=stake,
            purpose="duel",
            reference_type="duel",
            reference_id=str(duel.pk),
            # Slightly longer than the duel itself, so the wallet's own sweeper
            # is a backstop for this context's sweeper rather than a race.
            expires_in=DUEL_EXPIRY + timedelta(hours=1),
        )
        duel.challenger_hold_id = hold.pk
        duel.save(update_fields=["challenger_hold_id", "updated_at"])

        policy.record_wager(challenger, stake, reference=f"duel:{duel.pk}")

        bus.publish(
            DuelChallenged(
                duel_id=duel.id,
                challenger_id=challenger.id,
                defender_id=defender.id,
                stake_minor=stake.minor,
                asset=stake.asset,
                expires_at=duel.expires_at,
            )
        )
        handle.result_ref = str(duel.pk)
        return duel


@transaction.atomic
def accept_duel(*, duel: Duel, defender, client_seed: str = "", idempotency_key: str) -> Duel:
    """Match the stake and settle it in one transaction.

    Pot conservation is the invariant: both holds are captured, the rake is
    taken with `core.money.split_percentage` — whose remainder-absorbing split
    cannot lose a scaglia — and the rest becomes a pending credit for the
    winner. There is no arrangement of outcomes where the two halves fail to add
    back up to the pot.
    """
    duel = _lock_pending_duel(duel)

    if defender.pk != duel.defender_id:
        raise IllegalStateTransition(
            current="not_yours", attempted="accept", message="Questa sfida non è tua."
        )

    stake = duel.stake
    policy.assert_may_accept(defender, stake)

    with idempotent(
        idempotency_key, scope="games.duel_accept", payload={"duel": str(duel.pk)}
    ) as handle:
        if handle.replayed:
            return Duel.objects.get(pk=handle.result_ref)

        defender_hold = place_hold(
            user=defender,
            amount=stake,
            purpose="duel",
            reference_type="duel",
            reference_id=str(duel.pk),
        )
        duel.defender_hold_id = defender_hold.pk

        definition = get_game(GameKey.DUEL)
        proof = duel.proof or fairness.commit(user=defender, purpose="duel")
        fairness.bind(proof, client_seed=client_seed)
        outcome = definition.derive(proof.rng())

        challenger_wins = bool(outcome.detail["challenger_wins"])
        winner = duel.challenger if challenger_wins else duel.defender
        loser = duel.defender if challenger_wins else duel.challenger

        rake, to_winner = duel.split_pot()

        # Both stakes join the house bankroll and the winner's share comes back
        # out of it, exactly as `settle_risked_claim` does for a minigame. The
        # rake is what stays behind, and `recognise_revenue` carves charity and
        # redistribution out of it in the same transaction.
        capture_hold(
            _HoldRef(duel.challenger_hold_id),
            to=AccountKind.HOUSE_REVENUE,
            kind=EntryKind.DUEL_SETTLEMENT,
        )
        capture_hold(
            _HoldRef(duel.defender_hold_id),
            to=AccountKind.HOUSE_REVENUE,
            kind=EntryKind.DUEL_SETTLEMENT,
        )

        credit_pending(
            user=winner,
            amount=to_winner,
            source=CreditSource.DUEL_WIN,
            from_account=AccountKind.HOUSE_REVENUE,
            idempotency_key=f"duel-win:{duel.pk}",
            reference_type="duel",
            reference_id=str(duel.pk),
        )
        recognise_revenue(amount=rake, source="duel_rake", reference_id=duel.pk)

        # The round is booked from the challenger's seat so that every GameRound
        # in the system reads the same way: this user staked X and got Y back.
        challenger_payout = to_winner.minor if challenger_wins else 0
        game_round = GameRound.objects.create(
            user=duel.challenger,
            game_key=GameKey.DUEL,
            source=RoundSource.DUEL,
            state=RoundState.SETTLED,
            stake_minor=stake.minor,
            asset=stake.asset,
            payout_minor=challenger_payout,
            multiplier_bp=challenger_payout * ONE_X_BP // stake.minor,
            proof=proof,
            outcome=outcome.as_json(),
            settled_at=timezone.now(),
        )
        fairness.reveal(proof)

        duel.state = DuelState.RESOLVED
        duel.winner = winner
        duel.round = game_round
        duel.rake_minor = rake.minor
        duel.resolved_at = timezone.now()
        duel.save(
            update_fields=[
                "state", "winner", "round", "rake_minor", "resolved_at",
                "defender_hold_id", "updated_at",
            ]
        )

        policy.record_wager(defender, stake, reference=f"duel:{duel.pk}")
        policy.record_loss(loser, stake, Amount.zero(stake.asset), reference=f"duel:{duel.pk}")

        bus.publish(
            GameSettled(
                round_id=game_round.id,
                user_id=duel.challenger_id,
                game_key=str(GameKey.DUEL),
                stake_minor=stake.minor,
                payout_minor=challenger_payout,
                asset=stake.asset,
                fairness_proof_id=proof.id,
            )
        )
        bus.publish(
            DuelResolved(
                duel_id=duel.id,
                winner_id=winner.id,
                loser_id=loser.id,
                pot_minor=duel.pot.minor,
                asset=stake.asset,
                fairness_proof_id=proof.id,
            )
        )
        handle.result_ref = str(duel.pk)
        return duel


@transaction.atomic
def decline_duel(*, duel: Duel, defender) -> Duel:
    """Say no. The challenger's stake comes back in full — no fee for refusing.

    Charging anything to decline would make declining a decision with a price,
    and an opt-in mechanic whose refusal costs money is not opt-in.
    """
    duel = _lock_pending_duel(duel)

    if defender.pk != duel.defender_id:
        raise IllegalStateTransition(
            current="not_yours", attempted="decline", message="Questa sfida non è tua."
        )

    _release_holds(duel)
    duel.state = DuelState.DECLINED
    duel.resolved_at = timezone.now()
    duel.save(update_fields=["state", "resolved_at", "updated_at"])
    return duel


@transaction.atomic
def expire_duel(duel: Duel) -> Duel:
    """Nobody answered. Give the stake back and close it.

    Idempotent and safe to call on an already-resolved duel: the sweeper runs on
    a schedule and must not care whether it lost a race with a human.
    """
    duel = Duel.objects.select_for_update().get(pk=duel.pk)
    if duel.state != DuelState.PENDING:
        return duel

    _release_holds(duel)
    duel.state = DuelState.EXPIRED
    duel.resolved_at = timezone.now()
    duel.save(update_fields=["state", "resolved_at", "updated_at"])
    return duel


def _lock_pending_duel(duel: Duel) -> Duel:
    duel = Duel.objects.select_for_update().get(pk=duel.pk)
    if duel.state != DuelState.PENDING:
        raise IllegalStateTransition(
            current=duel.state,
            attempted="respond",
            message="Questa sfida non è più aperta.",
        )
    if duel.expires_at <= timezone.now():
        # Refuse, but leave the row alone: expiring it here would be undone by
        # the exception that has to follow, and `games.tasks.expire_stale_duels`
        # is already the one thing responsible for releasing the hold.
        raise IllegalStateTransition(
            current="expired", attempted="respond", message="Questa sfida è scaduta."
        )
    return duel


def _release_holds(duel: Duel) -> None:
    """Return both stakes. Every non-resolving exit calls exactly this.

    Both sides, always — the defender's hold is normally absent on a pending
    duel, but releasing an id that is None is a no-op and a duel that leaked one
    hold would be a bug nobody notices until somebody's money is missing.
    """
    for hold_id in (duel.challenger_hold_id, duel.defender_hold_id):
        if hold_id is not None:
            release_hold(_HoldRef(hold_id))


# ---------------------------------------------------------------------------
# Self-exclusion
# ---------------------------------------------------------------------------


def cancel_open_duels_for(user) -> int:
    """Close every pending duel this user is involved in, releasing both stakes.

    Called when they self-exclude. `SelfExclusionStarted` in `core.contracts`
    says handlers are expected to refund staked wagers rather than merely block
    login, and an open duel is a staked wager: leaving it pending would keep
    soliciting a response from somebody who has just asked to be left alone.
    """
    closed = 0
    for duel in Duel.objects.open_for(user):
        expire_duel(duel)
        closed += 1
    if closed:
        logger.info("Released %d open duel(s) for excluded user %s", closed, user.pk)
    return closed

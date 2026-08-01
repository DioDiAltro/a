"""Thin surfaces over `games.services`.

No view here decides anything. Each one resolves the objects, calls a service
and renders the result — the outcome is already committed by the time a template
is touched, which is what makes the canvas animations safe to make elaborate.

`history` and `verify` are the two routes `compliance.middleware` lets through
for a self-excluded user, and they are written accordingly: neither offers a way
to start a round, and `verify` needs no account at all.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.exceptions import BiglieError
from games import services
from games.constants import GameKey
from games.engine import LADDER_RUNGS, get_game
from games.fairness import (
    VERIFICATION_SNIPPET,
    VERIFICATION_STEPS,
    commitment_for,
    normalise_client_seed,
)
from games.forms import ChallengeForm, ClientSeedForm
from games.models import Duel
from wallet.selectors import get_claimable_credit

#: Where the player's chosen client seed lives between rounds. The session, not
#: a column: it is a preference, it must be changeable at any moment, and
#: nothing about a past round depends on the current value — every round stores
#: the seed it actually used on its own proof.
SEED_SESSION_KEY = "games:client_seed"


def _client_seed(request) -> str:
    seed = request.session.get(SEED_SESSION_KEY)
    if not seed:
        seed = normalise_client_seed(None)
        request.session[SEED_SESSION_KEY] = seed
    return seed


# ---------------------------------------------------------------------------
# Catalogue and the claim gamble
# ---------------------------------------------------------------------------


@login_required
def index(request):
    """Every game with its declared RTP and its full paytable, before you play.

    The odds are on the same page as the button, not behind a link in a footer.
    A player who has to go looking for the paytable has been told nothing.
    """
    return render(
        request,
        "games/index.html",
        {
            "games": services.available_games(),
            "commitment": commitment_for(request.user),
            "seed_form": ClientSeedForm(initial={"client_seed": _client_seed(request)}),
        },
    )


@login_required
def choose_gamble(request, credit_id: uuid.UUID):
    """Pick which game decides a pending credit.

    Reached from the dashboard's *Rischiale*. The safe claim stays one click
    away on this page too — the moment somebody has committed to gambling is
    exactly the moment the alternative should still be visible.
    """
    credit = get_claimable_credit(request.user, credit_id)
    if credit is None or not credit.is_claimable:
        messages.warning(request, "Questo riscatto non è più disponibile.")
        return redirect("web:dashboard")

    return render(
        request,
        "games/choose_gamble.html",
        {
            "credit": credit,
            "games": services.available_games(),
            "commitment": commitment_for(request.user),
            "seed_form": ClientSeedForm(initial={"client_seed": _client_seed(request)}),
        },
    )


@require_POST
@login_required
def play(request, credit_id: uuid.UUID):
    """Run the round. The outcome exists before the response is written."""
    game_key = request.POST.get("game_key", "")

    form = ClientSeedForm(request.POST)
    if form.is_valid() and form.cleaned_data.get("client_seed"):
        request.session[SEED_SESSION_KEY] = normalise_client_seed(
            form.cleaned_data["client_seed"]
        )

    try:
        game_round = services.play_claim_gamble(
            user=request.user,
            credit_id=credit_id,
            game_key=game_key,
            client_seed=_client_seed(request),
            idempotency_key=f"gamble:{credit_id}:{game_key}",
        )
    except BiglieError as exc:
        messages.error(request, exc.user_message)
        return redirect("web:dashboard")

    # A fresh commitment for the next round, published immediately so the player
    # can see the hash of the round they have not played yet.
    commitment_for(request.user)
    return redirect("games:round", round_id=game_round.pk)


@login_required
def round_detail(request, round_id: uuid.UUID):
    """The stage: canvas, verdict, and the fairness receipt underneath it."""
    game_round = services.get_round(request.user, round_id)
    if game_round is None:
        raise Http404

    definition = get_game(game_round.game_key)
    return render(
        request,
        "games/stage.html",
        {
            "round": game_round,
            "definition": definition,
            "payload": services.animation_payload(game_round),
            "ladder_rungs": LADDER_RUNGS,
            "is_ladder": game_round.game_key == GameKey.LADDER,
        },
    )


@login_required
def round_payload(request, round_id: uuid.UUID):
    """The animation payload as JSON, for the canvas and for the socket relay."""
    game_round = services.get_round(request.user, round_id)
    if game_round is None:
        raise Http404
    return JsonResponse(services.animation_payload(game_round))


# ---------------------------------------------------------------------------
# Ladder
# ---------------------------------------------------------------------------


@require_POST
@login_required
def ladder_step(request, round_id: uuid.UUID):
    try:
        services.ladder_step(user=request.user, round_id=round_id)
    except BiglieError as exc:
        messages.warning(request, exc.user_message)
    return redirect("games:round", round_id=round_id)


@require_POST
@login_required
def ladder_cash_out(request, round_id: uuid.UUID):
    try:
        services.ladder_cash_out(user=request.user, round_id=round_id)
    except BiglieError as exc:
        messages.warning(request, exc.user_message)
    return redirect("games:round", round_id=round_id)


# ---------------------------------------------------------------------------
# History and verification — both reachable while self-excluded
# ---------------------------------------------------------------------------


@login_required
def history(request):
    """What actually happened, including the losses.

    Allowlisted in `compliance.middleware`: somebody who has excluded themselves
    still has every right to look at what they played and check it.
    """
    rounds = services.history_for(request.user)
    return render(
        request,
        "games/history.html",
        {
            "rounds": rounds,
            "staked_minor": sum(r.stake_minor for r in rounds),
            "returned_minor": sum(r.payout_minor or 0 for r in rounds),
        },
    )


def verify(request, round_id: uuid.UUID):
    """Re-derive a round from its revealed seed. Public, and deliberately so.

    No login: a fairness proof only one person can inspect is a weaker claim
    than one anybody can. The page shows the commitment, the seeds, the exact
    HMAC message and a self-contained script, then states plainly whether the
    stored outcome matches the replay.
    """
    game_round = services.get_round_for_verification(round_id)
    if game_round is None:
        raise Http404

    proof = game_round.proof
    return render(
        request,
        "games/verify.html",
        {
            "round": game_round,
            "proof": proof,
            "fairness": proof.public_dict(),
            "verified": services.verify_round(game_round) if proof.is_revealed else None,
            "steps": VERIFICATION_STEPS,
            "snippet": VERIFICATION_SNIPPET,
            "outcome": game_round.public_detail(),
        },
    )


@require_POST
@login_required
def set_client_seed(request):
    """Change the client seed. Always allowed, takes effect on the next round."""
    form = ClientSeedForm(request.POST)
    if form.is_valid():
        request.session[SEED_SESSION_KEY] = normalise_client_seed(
            form.cleaned_data.get("client_seed")
        )
        messages.success(request, "Seme aggiornato. Vale dalla prossima partita.")
    return redirect(request.POST.get("next") or "games:index")


# ---------------------------------------------------------------------------
# Duels
# ---------------------------------------------------------------------------


@login_required
def duels(request):
    return render(
        request,
        "games/duels.html",
        {
            "open_duels": Duel.objects.open_for(request.user),
            "resolved": Duel.objects.resolved_for(request.user)[:20],
            # A per-render nonce, so a double-submitted form is one challenge
            # while a deliberate second challenge is a second one.
            "form": ChallengeForm(initial={"nonce": uuid.uuid4()}),
            "rake_percent": 4,
        },
    )


@login_required
def duel_detail(request, duel_id: uuid.UUID):
    duel = get_object_or_404(Duel, pk=duel_id)
    if request.user.pk not in (duel.challenger_id, duel.defender_id):
        raise Http404
    return render(request, "games/duel_detail.html", {"duel": duel})


@require_POST
@login_required
def duel_challenge(request):
    form = ChallengeForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Controlla i dati della sfida.")
        return redirect("games:duels")

    try:
        duel = services.challenge(
            challenger=request.user,
            defender=form.cleaned_data["defender"],
            stake=form.cleaned_data["amount"],
            message=form.cleaned_data.get("message", ""),
            idempotency_key=f"duel:{request.user.pk}:{form.cleaned_data.get('nonce') or uuid.uuid4()}",
        )
    except BiglieError as exc:
        messages.error(request, exc.user_message)
        return redirect("games:duels")

    messages.success(request, "Sfida inviata. La posta è bloccata finché non risponde.")
    return redirect("games:duel_detail", duel_id=duel.pk)


@require_POST
@login_required
def duel_accept(request, duel_id: uuid.UUID):
    duel = get_object_or_404(Duel, pk=duel_id)
    try:
        services.accept_duel(
            duel=duel,
            defender=request.user,
            client_seed=_client_seed(request),
            idempotency_key=f"duel-accept:{duel.pk}",
        )
    except BiglieError as exc:
        messages.error(request, exc.user_message)
    return redirect("games:duel_detail", duel_id=duel.pk)


@require_POST
@login_required
def duel_decline(request, duel_id: uuid.UUID):
    duel = get_object_or_404(Duel, pk=duel_id)
    try:
        services.decline_duel(duel=duel, defender=request.user)
        messages.success(request, "Sfida rifiutata. La posta è tornata a chi ti ha sfidato.")
    except BiglieError as exc:
        messages.error(request, exc.user_message)
    return redirect("games:duels")

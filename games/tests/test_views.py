"""The surfaces, and the two route names other contexts depend on.

`web.views.claim_credit_risky` redirects to ``games:choose_gamble`` and every
settled claim card links to ``games:verify``. Renaming either would break a
template in another app at runtime rather than at import, so both are pinned
here.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from core.money import Amount
from games import services
from games.constants import GameKey
from games.tests.conftest import VINTA

STAKE = Amount(500, VINTA)


@pytest.fixture
def logged_in(client, player):
    client.force_login(player)
    return client


# ---------------------------------------------------------------------------
# The names other contexts reverse
# ---------------------------------------------------------------------------


def test_the_claim_funnel_hands_off_to_choose_gamble(db, logged_in, player, pending_credit):
    """`web` redirects here with a `credit_id`. This is that hand-off."""
    credit = pending_credit(player, STAKE)

    response = logged_in.get(reverse("web:claim_risky", args=[credit.pk]))
    assert response.status_code == 302
    assert response.headers["Location"] == reverse(
        "games:choose_gamble", kwargs={"credit_id": credit.pk}
    )

    page = logged_in.get(response.headers["Location"])
    assert page.status_code == 200
    # The odds are on the page with the button, not behind a link.
    assert b"RTP" in page.content
    # And so is the way out.
    assert reverse("web:claim_safe", args=[credit.pk]).encode() in page.content


def test_the_verify_route_takes_a_round_id_positionally(db, player, pending_credit):
    """`web/templates/web/partials/claim_resolved.html` uses the positional form."""
    credit = pending_credit(player, STAKE)
    game_round = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.WHEEL,
        client_seed="x", idempotency_key="v1",
    )
    assert reverse("games:verify", args=[game_round.pk])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_the_catalogue_shows_every_game_with_its_odds(db, logged_in):
    response = logged_in.get(reverse("games:index"))
    assert response.status_code == 200
    for name in (b"Slot", b"Ruota", b"Scala", b"Testa o croce"):
        assert name in response.content


def test_playing_lands_on_the_stage(db, logged_in, player, pending_credit):
    credit = pending_credit(player, STAKE)

    response = logged_in.post(
        reverse("games:play", kwargs={"credit_id": credit.pk}),
        {"game_key": GameKey.SLOT},
    )
    assert response.status_code == 302

    stage = logged_in.get(response.headers["Location"])
    assert stage.status_code == 200
    # The verdict is in the document before any script runs: the outcome is the
    # page, the animation is the gloss.
    assert b"stage__verdict" in stage.content
    assert b"round-payload" in stage.content


def test_the_verify_page_is_public(db, client, player, pending_credit):
    """A proof only the loser can inspect is a weaker claim than a shareable one."""
    credit = pending_credit(player, STAKE)
    game_round = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.COINFLIP,
        client_seed="pubblico", idempotency_key="v2",
    )

    response = client.get(reverse("games:verify", args=[game_round.pk]))
    assert response.status_code == 200
    assert b"verificata" in response.content
    assert game_round.proof.server_seed.encode() in response.content, "the seed was not revealed"


def test_history_and_verify_stay_open_to_a_self_excluded_user(
    db, logged_in, player, pending_credit
):
    """`compliance.middleware` allowlists both. Blocking them would make
    self-exclusion punitive rather than protective."""
    credit = pending_credit(player, STAKE)
    game_round = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.SLOT,
        client_seed="x", idempotency_key="v3",
    )

    player.is_self_excluded = True
    player.save(update_fields=["is_self_excluded"])

    assert logged_in.get(reverse("games:history")).status_code == 200
    assert logged_in.get(reverse("games:verify", args=[game_round.pk])).status_code == 200

    # ...while anything that would take a stake is not.
    blocked = logged_in.get(reverse("games:index"))
    assert blocked.status_code == 302


def test_the_client_seed_is_the_players_to_change(db, logged_in):
    response = logged_in.post(
        reverse("games:client_seed"),
        {"client_seed": "cambiato-da-me", "next": reverse("games:index")},
    )
    assert response.status_code == 302
    assert logged_in.session["games:client_seed"] == "cambiato-da-me"


def test_the_round_payload_endpoint_is_scoped_to_its_owner(
    db, client, player, rival, pending_credit
):
    credit = pending_credit(player, STAKE)
    game_round = services.play_claim_gamble(
        user=player, credit_id=credit.pk, game_key=GameKey.WHEEL,
        client_seed="x", idempotency_key="v4",
    )

    client.force_login(rival)
    assert client.get(reverse("games:round_payload", args=[game_round.pk])).status_code == 404

    client.force_login(player)
    payload = client.get(reverse("games:round_payload", args=[game_round.pk])).json()
    assert payload["round_id"] == str(game_round.pk)


def test_the_duels_page_renders_for_someone_with_no_duels(db, logged_in):
    assert logged_in.get(reverse("games:duels")).status_code == 200

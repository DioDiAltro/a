"""Routes for the games context.

Two names here are load-bearing outside this app and must not be renamed:

* ``games:choose_gamble`` — `web.views.claim_credit_risky` redirects here with a
  ``credit_id``. It is the hand-off from the claim funnel into the games.
* ``games:verify`` — linked from every settled claim card, and allowlisted in
  `compliance.middleware` alongside ``games:history`` so that a self-excluded
  user can still read their history and check a past result. Blocking those two
  would make self-exclusion punitive rather than protective.
"""

from django.urls import path

from games import views

app_name = "games"

urlpatterns = [
    path("", views.index, name="index"),
    # -- the claim gamble ---------------------------------------------------
    path("rischia/<uuid:credit_id>/", views.choose_gamble, name="choose_gamble"),
    path("rischia/<uuid:credit_id>/gioca/", views.play, name="play"),
    path("partita/<uuid:round_id>/", views.round_detail, name="round"),
    path("partita/<uuid:round_id>/payload/", views.round_payload, name="round_payload"),
    # -- the ladder's two decisions ----------------------------------------
    path("partita/<uuid:round_id>/sali/", views.ladder_step, name="ladder_step"),
    path("partita/<uuid:round_id>/incassa/", views.ladder_cash_out, name="ladder_cash_out"),
    # -- reachable while self-excluded -------------------------------------
    path("storico/", views.history, name="history"),
    path("verifica/<uuid:round_id>/", views.verify, name="verify"),
    path("seme/", views.set_client_seed, name="client_seed"),
    # -- duels --------------------------------------------------------------
    path("sfide/", views.duels, name="duels"),
    path("sfide/nuova/", views.duel_challenge, name="duel_challenge"),
    path("sfide/<uuid:duel_id>/", views.duel_detail, name="duel_detail"),
    path("sfide/<uuid:duel_id>/accetta/", views.duel_accept, name="duel_accept"),
    path("sfide/<uuid:duel_id>/rifiuta/", views.duel_decline, name="duel_decline"),
]

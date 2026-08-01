"""Marketplace routes.

`order_list` and `order_detail` are named exactly that because
`compliance.middleware.ALWAYS_ALLOWED_ROUTES` allowlists them: a user who has
self-excluded must still be able to see what they bought, chase a parcel and
confirm a delivery. Renaming either of these silently locks excluded users out
of their own orders, so the names are part of the contract.
"""

from django.urls import path

from marketplace import views

app_name = "marketplace"

urlpatterns = [
    path("", views.listing_list, name="listing_list"),
    path("vendi/", views.listing_create, name="listing_create"),
    path("negozio/", views.seller_dashboard_view, name="seller_dashboard"),

    path("annuncio/<uuid:pk>/", views.listing_detail, name="listing_detail"),
    path("annuncio/<uuid:pk>/partecipa/", views.participate, name="participate"),
    path("annuncio/<uuid:pk>/annuncia/", views.announce_draw_view, name="announce_draw"),
    path("annuncio/<uuid:pk>/chiudi/", views.settle_now_view, name="settle_now"),

    #: Public: anybody can check a draw, account or no account.
    path("estrazione/<uuid:pk>/verifica/", views.sale_verify, name="sale_verify"),

    # -- allowlisted by the self-exclusion middleware ------------------------
    path("ordini/", views.order_list, name="order_list"),
    path("ordini/<uuid:pk>/", views.order_detail, name="order_detail"),
    path("ordini/<uuid:pk>/spedito/", views.order_ship, name="order_ship"),
    path("ordini/<uuid:pk>/conferma/", views.order_confirm, name="order_confirm"),
    path("ordini/<uuid:pk>/contesta/", views.order_dispute, name="order_dispute"),
]

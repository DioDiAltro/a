"""Routes for the real-money surfaces.

`withdraw` and `withdraw_confirm` are named exactly that on purpose:
`compliance.middleware.ALWAYS_ALLOWED_ROUTES` allowlists those two names inside
the otherwise-restricted `payments` namespace, so a self-excluded user can still
take their money out. Renaming either one silently locks people away from their
own balance, which is why the names are load-bearing and a test asserts them.
"""

from django.urls import path

from payments import views, webhooks

app_name = "payments"

urlpatterns = [
    # -- money in ----------------------------------------------------------
    path("ricarica/", views.deposit, name="deposit"),
    path("ricarica/<uuid:pk>/", views.deposit_checkout, name="deposit_checkout"),
    # Dev affordance only: 404s unless the provider is the fake one and
    # LIVE_MODE is off. See `views.deposit_simulate`.
    path("ricarica/<uuid:pk>/simula/", views.deposit_simulate, name="deposit_simulate"),
    # -- money out ---------------------------------------------------------
    path("preleva/", views.withdraw, name="withdraw"),
    path("preleva/<uuid:pk>/", views.withdraw_confirm, name="withdraw_confirm"),
    # -- history -----------------------------------------------------------
    path("movimenti/", views.history, name="history"),
    # -- provider callback -------------------------------------------------
    # No CSRF (the caller is the acquirer, not a browser) and no session: the
    # only thing that authenticates this request is the signature over its body.
    path("webhook/stripe/", webhooks.stripe_webhook, name="stripe_webhook"),
]

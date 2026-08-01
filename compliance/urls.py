from django.urls import path

from compliance import views

app_name = "compliance"

urlpatterns = [
    path("limiti/", views.limits, name="limits"),
    path("limiti/<str:kind>/<str:period>/annulla/", views.cancel_limit_change,
         name="cancel_limit_change"),
    path("autoesclusione/", views.self_exclusion, name="self_exclusion"),
    path("autoesclusione/stato/", views.self_exclusion_status, name="self_exclusion_status"),
    path("autoesclusione/termina/", views.end_exclusion, name="end_exclusion"),
    path("verifica/", views.kyc, name="kyc"),
]

from django.urls import path

from identity import views

app_name = "identity"

urlpatterns = [
    path("registrati/", views.RegisterView.as_view(), name="register"),
    path("accedi/", views.LoginView.as_view(), name="login"),
    path("esci/", views.LogoutView.as_view(), name="logout"),
    path("profilo/", views.profile, name="profile"),
    path("profilo/venditore/", views.seller_profile, name="seller_profile"),
    path("livello/", views.level, name="level"),
    # Password reset, four steps as Django models them.
    path("password/recupera/", views.PasswordResetView.as_view(), name="password_reset"),
    path(
        "password/recupera/inviata/",
        views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "password/recupera/<uidb64>/<token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "password/recupera/completata/",
        views.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    path("password/cambia/", views.PasswordChangeView.as_view(), name="password_change"),
    path(
        "password/cambia/fatto/",
        views.PasswordChangeDoneView.as_view(),
        name="password_change_done",
    ),
]

"""Account views: registration, session, password, profile.

Everything here is thin. Rules live in `identity.services` so that an API or a
management command reaches the same rules without going through a form.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import FormView

from core.exceptions import AgeVerificationRequired
from identity.forms import (
    EmailAuthenticationForm,
    ItalianPasswordResetForm,
    ItalianSetPasswordForm,
    ProfileForm,
    RegistrationForm,
    SellerProfileForm,
)
from identity.levels import LEVELS, level_of
from identity.models import SellerProfile
from identity.services import register_user


class RegisterView(FormView):
    template_name = "identity/register.html"
    form_class = RegistrationForm
    success_url = reverse_lazy("identity:profile")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("identity:profile")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            user = register_user(
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password1"],
                date_of_birth=form.cleaned_data["date_of_birth"],
                display_name=form.cleaned_data.get("display_name", ""),
            )
        except AgeVerificationRequired as exc:
            # The form should have caught this; if the service disagrees, the
            # service wins and the user is told why rather than shown a 500.
            form.add_error("date_of_birth", str(exc) or exc.user_message)
            return self.form_invalid(form)

        auth_login(self.request, user)
        messages.success(
            self.request,
            "Benvenuto su biglie. Imposta subito i tuoi limiti di gioco: "
            "sono la cosa più utile che puoi fare nei primi cinque minuti.",
        )
        return redirect(self.get_success_url())


class LoginView(auth_views.LoginView):
    template_name = "identity/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = True


class LogoutView(auth_views.LogoutView):
    """POST-only, as of Django 5. A link cannot log you out."""


class PasswordResetView(auth_views.PasswordResetView):
    template_name = "identity/password_reset.html"
    email_template_name = "identity/password_reset_email.txt"
    subject_template_name = "identity/password_reset_subject.txt"
    form_class = ItalianPasswordResetForm
    success_url = reverse_lazy("identity:password_reset_done")


class PasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "identity/password_reset_done.html"


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "identity/password_reset_confirm.html"
    form_class = ItalianSetPasswordForm
    success_url = reverse_lazy("identity:password_reset_complete")


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "identity/password_reset_complete.html"


class PasswordChangeView(auth_views.PasswordChangeView):
    template_name = "identity/password_change.html"
    success_url = reverse_lazy("identity:password_change_done")


class PasswordChangeDoneView(auth_views.PasswordChangeDoneView):
    template_name = "identity/password_change_done.html"


@login_required
def profile(request):
    """Edit the handful of things a user owns about themselves."""
    form = ProfileForm(request.POST or None, request.FILES or None, instance=request.user)

    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profilo aggiornato.")
        return redirect("identity:profile")

    return render(
        request,
        "identity/profile.html",
        {
            "form": form,
            "level": level_of(request.user),
            "seller_profile": SellerProfile.objects.filter(user=request.user).first(),
        },
    )


@login_required
def seller_profile(request):
    """Open or edit a shop. Creating one is an explicit act, never implicit."""
    instance = SellerProfile.objects.filter(user=request.user).first()
    form = SellerProfileForm(request.POST or None, instance=instance)

    if request.method == "POST" and form.is_valid():
        profile_row = form.save(commit=False)
        profile_row.user = request.user
        profile_row.save()
        messages.success(
            request,
            "Negozio aperto." if instance is None else "Negozio aggiornato.",
        )
        return redirect("identity:seller_profile")

    return render(
        request,
        "identity/seller_profile.html",
        {"form": form, "seller_profile": instance, "level": level_of(request.user)},
    )


@login_required
def level(request):
    """The progression page: what the user has, and what the next level unlocks."""
    row = level_of(request.user)
    return render(
        request,
        "identity/level.html",
        {"level": row, "levels": LEVELS, "current": row.definition, "next": row.next_definition},
    )

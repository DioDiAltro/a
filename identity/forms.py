"""Forms for registration, login and profile editing.

All labels, help texts and errors are Italian: they are user-facing. The code
around them is English.
"""

from __future__ import annotations

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm, SetPasswordForm
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from identity.models import SellerProfile, User
from identity.services import MINIMUM_AGE, age_in_years


class DateInput(forms.DateInput):
    input_type = "date"


class RegistrationForm(forms.ModelForm):
    """Email, a password, a display name and — non-negotiably — a date of birth.

    The date of birth is a required field rather than an optional profile
    detail. Everything on this platform that can take money from a user is
    gated on `User.is_of_age`, and a null birthday would gate it shut anyway;
    asking at the door is the honest version.
    """

    email = forms.EmailField(
        label=_("Email"),
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}),
    )
    date_of_birth = forms.DateField(
        label="Data di nascita",
        widget=DateInput(attrs={"autocomplete": "bday"}),
        help_text=f"Devi avere almeno {MINIMUM_AGE} anni.",
    )
    password1 = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Conferma password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Riscrivi la password per conferma.",
    )
    accepts_terms = forms.BooleanField(
        label="Dichiaro di essere maggiorenne e accetto i termini di servizio",
        required=True,
        error_messages={
            "required": "Devi accettare i termini per registrarti.",
        },
    )

    class Meta:
        model = User
        fields = ("email", "display_name", "date_of_birth")
        labels = {"display_name": "Nome visualizzato"}
        help_texts = {
            "display_name": "Come ti vedranno gli altri utenti. Puoi cambiarlo dopo.",
        }

    def clean_email(self) -> str:
        email = User.objects.normalize_email(self.cleaned_data["email"])
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Esiste già un account con questa email.")
        return email

    def clean_date_of_birth(self):
        dob = self.cleaned_data["date_of_birth"]
        today = timezone.localdate()

        if dob > today:
            raise forms.ValidationError("La data di nascita non può essere nel futuro.")

        age = age_in_years(dob, today)
        if age < MINIMUM_AGE:
            raise forms.ValidationError(
                f"Devi avere almeno {MINIMUM_AGE} anni per registrarti su biglie. "
                "Questa piattaforma gestisce denaro reale e giochi con premi."
            )
        if age > 120:
            raise forms.ValidationError("Controlla la data di nascita.")
        return dob

    def clean_password2(self) -> str:
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("Le due password non coincidono.")
        password_validation.validate_password(password2, self.instance)
        return password2


class EmailAuthenticationForm(AuthenticationForm):
    """`AuthenticationForm` with the username field told the truth: it is an email."""

    username = forms.EmailField(
        label=_("Email"),
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}),
    )

    error_messages = {
        "invalid_login": "Email o password non corretti.",
        "inactive": "Questo account è disattivato.",
    }


class ItalianPasswordResetForm(PasswordResetForm):
    email = forms.EmailField(
        label=_("Email"),
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )


class ItalianSetPasswordForm(SetPasswordForm):
    error_messages = {
        **SetPasswordForm.error_messages,
        "password_mismatch": "Le due password non coincidono.",
    }
    new_password1 = forms.CharField(
        label="Nuova password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "autofocus": True}),
    )
    new_password2 = forms.CharField(
        label="Conferma nuova password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )


class ProfileForm(forms.ModelForm):
    """What a user may change about themselves after registration.

    Date of birth is deliberately absent: it is an identity attribute that KYC
    checks against a document, and a field the user can edit freely is a field
    that defeats the age gate.
    """

    class Meta:
        model = User
        fields = ("display_name", "avatar")
        labels = {"display_name": "Nome visualizzato", "avatar": "Immagine del profilo"}


class SellerProfileForm(forms.ModelForm):
    class Meta:
        model = SellerProfile
        fields = ("shop_name", "bio", "accepts_duels")
        labels = {
            "shop_name": "Nome del negozio",
            "bio": "Descrizione",
            "accepts_duels": "Accetto le sfide di altri venditori",
        }
        help_texts = {
            "accepts_duels": (
                "Se lo disattivi nessuno può sfidarti. Puoi cambiare idea quando vuoi."
            ),
        }
        widgets = {"bio": forms.Textarea(attrs={"rows": 4})}

    def clean_shop_name(self) -> str:
        name = self.cleaned_data["shop_name"].strip()
        if len(name) < 3:
            raise forms.ValidationError("Il nome del negozio deve avere almeno 3 caratteri.")
        clash = SellerProfile.objects.filter(shop_name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(user_id=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Questo nome è già usato da un altro venditore.")
        return name

"""Input parsing for the game surfaces.

Forms here validate *shape*, never eligibility. Whether a user may stake what
they typed is `games.policy`'s answer, and duplicating any part of that decision
in a form would create a second place for it to be wrong.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib.auth import get_user_model

from core.money import Amount, Asset
from games.constants import CLIENT_SEED_MAX_LENGTH, MIN_DUEL_STAKE_MINOR

WAGERABLE_CHOICES = [(a.value, a.label) for a in Asset if a.is_wagerable]


class ClientSeedForm(forms.Form):
    """The player's own entropy. Changing it is always allowed.

    Deliberately unvalidated beyond a length cap: it is *their* contribution and
    constraining its content would only narrow the space we are asking them to
    trust.
    """

    client_seed = forms.CharField(
        max_length=CLIENT_SEED_MAX_LENGTH,
        required=False,
        label="Il tuo seme",
        help_text="Scegli qualsiasi testo. Puoi cambiarlo prima di ogni partita.",
    )


class ChallengeForm(forms.Form):
    """Send a duel. Resolves the opponent by email, which is what a seller knows."""

    defender = forms.EmailField(label="Email del venditore")
    stake = forms.CharField(label="Posta")
    asset = forms.ChoiceField(choices=WAGERABLE_CHOICES, label="Con quali biglie")
    message = forms.CharField(max_length=140, required=False, label="Due parole")
    nonce = forms.CharField(widget=forms.HiddenInput, required=False)

    def clean_defender(self):
        email = self.cleaned_data["defender"]
        user = get_user_model().objects.filter(email__iexact=email).first()
        if user is None:
            raise forms.ValidationError("Nessun utente con questa email.")
        return user

    def clean(self):
        cleaned = super().clean()
        raw = (cleaned.get("stake") or "").replace(",", ".").strip()
        asset = cleaned.get("asset")
        if not raw or not asset:
            return cleaned

        try:
            # Parsed as a string, never a float: `Amount.from_major` refuses
            # floats on purpose and this is the boundary it exists to guard.
            amount = Amount.from_major(Decimal(raw), Asset(asset))
        except (InvalidOperation, ArithmeticError, ValueError):
            self.add_error("stake", "Importo non valido.")
            return cleaned

        if amount.minor < MIN_DUEL_STAKE_MINOR:
            self.add_error("stake", "Posta troppo bassa per una sfida.")
        cleaned["amount"] = amount
        return cleaned

"""Forms for the two real-money surfaces.

Amounts are collected as decimals and converted to `Amount` here, at the edge,
so nothing downstream ever sees a fractional type. The IBAN is validated with
the real mod-97 check rather than a regex: telling someone their payout failed
three days later because of a typo we could have caught is not acceptable on
the one screen that has to work.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django import forms

from core.money import Amount, Asset
from payments.constants import (
    MAX_DEPOSIT_MINOR,
    MAX_WITHDRAWAL_MINOR,
    MIN_DEPOSIT_MINOR,
    MIN_WITHDRAWAL_MINOR,
)

IBAN_PATTERN = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}$")


def iban_is_valid(candidate: str) -> bool:
    """ISO 13616 mod-97 check. Catches transposed digits, not just wrong shapes."""
    normalised = "".join(candidate.split()).upper()
    if not IBAN_PATTERN.match(normalised):
        return False

    rotated = normalised[4:] + normalised[:4]
    digits = "".join(
        str(ord(char) - 55) if char.isalpha() else char for char in rotated
    )
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


class AmountField(forms.DecimalField):
    """A human figure in major units, handed on as integer minor units."""

    def __init__(self, *, asset: Asset, **kwargs):
        self.asset = asset
        kwargs.setdefault("max_digits", 9)
        kwargs.setdefault("decimal_places", 2)
        super().__init__(**kwargs)

    def clean(self, value) -> Amount:
        value = super().clean(value)
        try:
            # str() then Decimal: the one conversion that never picks up a
            # float's rounding error on the way in.
            return Amount.from_major(Decimal(str(value)), self.asset)
        except (InvalidOperation, TypeError) as exc:
            raise forms.ValidationError("Importo non valido.") from exc


class DepositForm(forms.Form):
    """How much real money to put in."""

    euro = AmountField(
        asset=Asset.EUR,
        label="Quanto vuoi ricaricare",
        min_value=Decimal(MIN_DEPOSIT_MINOR) / 100,
        max_value=Decimal(MAX_DEPOSIT_MINOR) / 100,
        help_text="1 € = 1 biglia. Nessuna commissione sulla ricarica.",
        widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
    )


class WithdrawalForm(forms.Form):
    """How many biglie piene to turn back into euros, and where to send them."""

    biglie = AmountField(
        asset=Asset.BIGLIA_PIENA,
        label="Quante biglie piene vuoi prelevare",
        min_value=Decimal(MIN_WITHDRAWAL_MINOR) / 100,
        max_value=Decimal(MAX_WITHDRAWAL_MINOR) / 100,
        widget=forms.NumberInput(attrs={"step": "0.01", "inputmode": "decimal"}),
    )
    iban = forms.CharField(
        label="IBAN del conto intestato a te",
        max_length=34,
        help_text=(
            "Il conto deve essere intestato alla stessa persona del profilo. "
            "Non conserviamo l'IBAN: teniamo solo le ultime quattro cifre e "
            "un'impronta non reversibile."
        ),
    )

    def clean_iban(self) -> str:
        iban = "".join(self.cleaned_data["iban"].split()).upper()
        if not iban_is_valid(iban):
            raise forms.ValidationError("Questo IBAN non è valido. Controlla le cifre.")
        return iban


class WithdrawalDecisionForm(forms.Form):
    """The reviewer's verdict, used by the staff queue in the admin."""

    reason = forms.CharField(
        label="Motivo", max_length=255, required=False,
        widget=forms.TextInput(attrs={"placeholder": "Visibile all'utente"}),
    )

"""Forms for the responsible-play surfaces."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django import forms
from django.utils import timezone

from compliance.constants import LimitKind, LimitPeriod
from core.money import Amount, Asset


class LimitForm(forms.Form):
    kind = forms.ChoiceField(choices=LimitKind.choices, label="Cosa vuoi limitare")
    period = forms.ChoiceField(choices=LimitPeriod.choices, label="Ogni")
    value = forms.DecimalField(
        min_value=Decimal("0"), decimal_places=2, label="Massimo",
        help_text="In biglie. Per il tempo di gioco, in minuti.",
    )

    @property
    def value_minor(self) -> int:  # pragma: no cover - convenience accessor
        return self.cleaned_data["value_minor"]

    def clean(self):
        data = super().clean()
        value = data.get("value")
        kind = data.get("kind")
        if value is None or kind is None:
            return data

        # Session time is counted in whole minutes; money is counted in minor
        # units. Storing both as a bare integer means the unit has to be decided
        # exactly once, here.
        if kind == LimitKind.SESSION_TIME:
            data["value_minor"] = int(value)
        else:
            data["value_minor"] = Amount.from_major(str(value), Asset.BIGLIA_PIENA).minor

        return data


class SelfExclusionForm(forms.Form):
    DURATIONS = [
        ("1", "24 ore"),
        ("7", "7 giorni"),
        ("30", "30 giorni"),
        ("180", "6 mesi"),
        ("permanent", "Per sempre"),
    ]

    duration = forms.ChoiceField(choices=DURATIONS, label="Per quanto")
    reason = forms.CharField(required=False, widget=forms.Textarea, label="Vuoi dirci perché? (facoltativo)")
    confirm = forms.BooleanField(
        label="Ho capito che non potrò annullare questa scelta prima della scadenza.",
        error_messages={"required": "Devi confermare di aver capito."},
    )

    def clean(self):
        data = super().clean()
        duration = data.get("duration")
        if duration == "permanent":
            data["until"] = None
        elif duration:
            data["until"] = timezone.now() + timedelta(days=int(duration))
        return data

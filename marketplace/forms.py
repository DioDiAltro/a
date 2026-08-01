"""Forms for putting something up for sale.

The interesting problem here is the plugin seam meeting a web form: the modes
are extensible, so this form cannot know their fields. It therefore takes the
mechanic's configuration as JSON and hands it straight to
`SaleMode.validate_config`, which is the one place that knows what a lottery or
a mystery box actually needs — and which already answers in Italian, so its
refusals become field errors without translation.

A per-mode form class could be registered on the mode later; the seam is
already the right shape for it. What must not happen is this file growing an
`if mode == "lottery"`.
"""

from __future__ import annotations

import json

from django import forms

from core.exceptions import BiglieError
from core.money import Asset
from marketplace.constants import ListingKind
from marketplace.models import Listing
from salemodes import registry
from salemodes import services as sales


class ListingForm(forms.ModelForm):
    """Create a listing and its sale in one step."""

    sale_mode = forms.ChoiceField(label="Come vuoi venderlo", choices=())
    config_json = forms.CharField(
        label="Parametri della modalità",
        widget=forms.Textarea(attrs={"rows": 5, "spellcheck": "false"}),
        required=False,
        help_text="JSON. Ogni modalità dichiara i suoi parametri qui sotto.",
    )

    class Meta:
        model = Listing
        fields = [
            "title", "description", "kind", "asset", "payout_in_biglie",
            "image", "ships_from", "shipping_note", "digital_payload",
            "bet_question", "bet_resolution_source", "bet_resolves_at",
            "bet_counter_stake_minor",
        ]
        labels = {
            "title": "Titolo",
            "description": "Descrizione",
            "kind": "Cosa vendi",
            "asset": "In che biglie",
            "payout_in_biglie": "Voglio essere pagato in biglie",
            "image": "Foto",
            "ships_from": "Spedisce da",
            "shipping_note": "Note di spedizione",
            "digital_payload": "Cosa consegnare (chiave, link, codice)",
            "bet_question": "Su cosa si scommette",
            "bet_resolution_source": "Chi decide l'esito",
            "bet_resolves_at": "Quando si saprà",
            "bet_counter_stake_minor": "La tua posta a copertura",
        }
        help_texts = {
            "payout_in_biglie": (
                "In biglie la commissione è più bassa: il valore resta dentro. "
                "In euro è più alta."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sale_mode"].choices = registry.choices()
        # The chooser explains itself: each mode's own description, straight
        # from the registry, so a new mechanic documents itself in the UI.
        self.mode_help = [
            {"key": mode.key, "label": mode.label, "description": mode.description}
            for mode in registry.all_modes()
        ]

    def clean_config_json(self) -> dict:
        raw = (self.cleaned_data.get("config_json") or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise forms.ValidationError(f"JSON non valido: {exc.msg}.") from exc
        if not isinstance(parsed, dict):
            raise forms.ValidationError("I parametri devono essere un oggetto JSON.")
        return parsed

    def clean(self):
        cleaned = super().clean()
        mode_key = cleaned.get("sale_mode")
        config = cleaned.get("config_json") or {}

        if mode_key:
            try:
                cleaned["mode_config"] = sales.validate_config(mode_key, config)
            except BiglieError as exc:
                # The mode already refuses in Italian; surfacing its own words
                # beats a generic "configurazione non valida".
                self.add_error("config_json", str(exc))

        if cleaned.get("kind") == ListingKind.BET and not cleaned.get("bet_question"):
            self.add_error("bet_question", "Una scommessa ha bisogno di una proposizione.")

        if not cleaned.get("payout_in_biglie") and cleaned.get("asset") != Asset.BIGLIA_PIENA:
            self.add_error(
                "asset",
                "Per essere pagato in euro l'annuncio deve essere in biglie piene.",
            )

        return cleaned


class ShipmentForm(forms.Form):
    carrier = forms.CharField(label="Corriere", max_length=60, required=False)
    tracking_code = forms.CharField(label="Codice di tracciamento", max_length=80,
                                    required=False)


class DisputeForm(forms.Form):
    reason = forms.CharField(
        label="Cosa è andato storto",
        widget=forms.Textarea(attrs={"rows": 4}),
        max_length=2000,
    )

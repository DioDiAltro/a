"""The public face of the 3%.

No login anywhere in this module, deliberately. A transparency page you have to
sign up to read is an argument for not believing it.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404, render

from charity.constants import PayoutState
from charity.models import Beneficiary
from charity.selectors import transparency_summary
from core.money import Amount, Asset


def transparency(request):
    """Raised, still in the pool, and actually sent — all three, always together.

    Showing only "raccolti" would let the number drift into meaning "donati",
    which is the exact place where a charity feature stops being accounting and
    starts being marketing.
    """
    asset = _requested_asset(request)
    return render(request, "charity/transparency.html", transparency_summary(asset))


def beneficiary_detail(request, slug: str):
    """One organisation: who they are, and every payout we have made to them."""
    beneficiary = get_object_or_404(Beneficiary, slug=slug)
    payouts = list(beneficiary.payouts.exclude(state=PayoutState.CANCELLED)[:100])

    totals: dict[Asset, Amount] = {}
    for payout in payouts:
        if payout.state == PayoutState.PAID:
            asset = Asset(payout.asset)
            totals[asset] = totals.get(asset, Amount.zero(asset)) + payout.amount

    return render(
        request,
        "charity/beneficiary.html",
        {"beneficiary": beneficiary, "payouts": payouts, "totals": totals},
    )


def _requested_asset(request) -> Asset:
    """Let the page be pointed at another asset without trusting the query string."""
    raw = request.GET.get("valuta", "")
    try:
        return Asset(raw)
    except ValueError:
        return Asset.BIGLIA_PIENA

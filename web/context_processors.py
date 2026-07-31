"""Template context shared by every page."""

from __future__ import annotations

from core.money import Asset
from wallet.services import summary_for


def wallet_summary(request):
    """Put the user's balances and unclaimed count in every template.

    The unclaimed count drives the badge on the dashboard link. It is the single
    most important number in the interface: it is the user's unopened present,
    and the whole claim mechanic depends on them noticing it.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    return {"wallet": summary_for(user), "ASSETS": [a for a in Asset if a.is_biglia]}

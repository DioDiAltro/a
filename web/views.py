"""The shell: home, dashboard, and the claim funnel.

`web` is presentation only. It reads through selectors and writes exclusively by
calling other contexts' service functions — there is no domain logic in this
module, and any that appears here belongs somewhere else.
"""

from __future__ import annotations

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.exceptions import BiglieError
from core.money import Asset
from wallet.constants import AccountKind
from wallet.ledger import system_account
from wallet.models import PendingCredit
from wallet.services import claim_safe


def home(request):
    """The market. Deliberately browsable logged-out — a marketplace nobody can
    look at has no reason to be joined."""
    return render(request, "web/home.html")


@login_required
def dashboard(request):
    """The claim funnel.

    Every biglia that enters the platform surfaces here as an unclaimed credit,
    and the user chooses safe or risky for each one. This page is the product's
    centre of gravity, which is why it is the one screen `web` owns outright
    rather than delegating to a domain app.
    """
    credits = (
        PendingCredit.objects.claimable(request.user)
        .order_by("expires_at")[:50]
    )
    return render(
        request,
        "web/dashboard.html",
        {
            "credits": credits,
            # Offered per-credit rather than as a bulk action on purpose: a
            # "claim all" button would turn a deliberate choice into a chore to
            # dismiss, and the choice is the point.
            "has_credits": bool(credits),
        },
    )


@require_POST
@login_required
def claim_credit_safe(request, credit_id: uuid.UUID):
    """Take the credit without risking it. HTMX swaps the card in place."""
    credit = get_object_or_404(PendingCredit, pk=credit_id, user=request.user)

    try:
        settled = claim_safe(
            credit=credit,
            idempotency_key=f"claim-safe:{credit.pk}",
        )
    except BiglieError as exc:
        messages.error(request, exc.user_message)
        return redirect("web:dashboard")

    credit.refresh_from_db()

    if request.htmx:
        return render(
            request,
            "web/partials/claim_resolved.html",
            {"credit": credit, "settled": settled, "risked": False},
        )

    messages.success(request, f"Riscattate {settled}.")
    return redirect("web:dashboard")


@login_required
def claim_credit_risky(request, credit_id: uuid.UUID):
    """Hand the credit to a minigame.

    This view only *routes* — the outcome is decided by `games`, inside a
    transaction, before any pixel moves. The client is told what happened and
    animates toward it.
    """
    credit = get_object_or_404(PendingCredit, pk=credit_id, user=request.user)

    if not credit.is_claimable:
        messages.warning(request, "Questo riscatto non è più disponibile.")
        return redirect("web:dashboard")

    if not request.user.can_wager:
        messages.warning(
            request,
            "Non puoi giocare in questo momento. Puoi comunque riscattare senza rischio.",
        )
        return redirect("web:dashboard")

    return HttpResponseRedirect(
        reverse("games:choose_gamble", kwargs={"credit_id": credit.pk})
    )


def charity(request):
    """Public transparency, readable without an account.

    The figure is read live off the ledger's CHARITY_POOL balance rather than
    from a cached counter, because a charity number nobody can check is
    marketing rather than accounting.
    """
    pools = {
        asset: system_account(AccountKind.CHARITY_POOL, asset).balance
        for asset in Asset
    }
    return render(request, "web/charity.html", {"pools": pools})


def fairness(request):
    """How to verify a result yourself. Static, and linked from every round."""
    return render(request, "web/fairness.html")

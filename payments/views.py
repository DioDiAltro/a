"""The user-facing money surfaces.

Three screens: put money in, take money out, see what happened. They are thin —
every rule they enforce lives in `payments.services`, and every failure they
show the user is a typed domain exception carrying its own Italian sentence.

The withdrawal screens are reachable while self-excluded (the compliance
middleware allowlists `payments:withdraw` and `payments:withdraw_confirm`),
which is why the confirmation flow stays on those two routes instead of
redirecting to a status page an excluded user would be bounced away from.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from compliance.services import current_tier
from core.exceptions import BiglieError
from payments.constants import AUTO_APPROVE_MAX_MINOR, PaymentState, ProviderKey
from payments.forms import DepositForm, WithdrawalForm
from payments.models import Deposit, Withdrawal
from payments.providers import FakeProvider, get_provider
from payments.services import (
    NON_WITHDRAWABLE_EXPLANATION,
    available_piena,
    confirm_withdrawal,
    open_withdrawal,
    recent_movements,
    start_deposit,
)
from payments.webhooks import handle_event
from wallet.services import summary_for

logger = logging.getLogger("payments")


# ---------------------------------------------------------------------------
# Deposit
# ---------------------------------------------------------------------------


@login_required
def deposit(request):
    """Start a top-up. Creates the PaymentIntent; credits nothing."""
    form = DepositForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            created = start_deposit(user=request.user, euro=form.cleaned_data["euro"])
        except BiglieError as exc:
            messages.error(request, exc.user_message)
        else:
            return redirect("payments:deposit_checkout", pk=created.pk)

    return render(
        request,
        "payments/deposit.html",
        {
            "form": form,
            "deposits": Deposit.objects.filter(user=request.user).order_by("-created_at")[:5],
        },
    )


@login_required
def deposit_checkout(request, pk):
    """Where the user actually pays.

    In a live deployment this mounts the provider's payment element with the
    client secret. With the fake provider it offers a button that produces a
    genuinely signed webhook, so the whole path — intent, callback, signature,
    idempotency, mint — is walkable in dev without a Stripe account.
    """
    deposit_row = get_object_or_404(Deposit, pk=pk, user=request.user)
    provider = get_provider()

    return render(
        request,
        "payments/checkout.html",
        {
            "deposit": deposit_row,
            "publishable_key": getattr(settings, "STRIPE_PUBLISHABLE_KEY", ""),
            "can_simulate": (
                isinstance(provider, FakeProvider)
                and not getattr(settings, "LIVE_MODE", False)
                and deposit_row.state == PaymentState.PENDING
            ),
        },
    )


@require_POST
@login_required
def deposit_simulate(request, pk):
    """Dev only: pretend the user paid.

    Guarded twice — the provider must be the fake one *and* LIVE_MODE must be
    off — because a route that fabricates a settled payment is a mint if it ever
    reaches production.
    """
    provider = get_provider()
    if getattr(settings, "LIVE_MODE", False) or not isinstance(provider, FakeProvider):
        raise Http404

    deposit_row = get_object_or_404(Deposit, pk=pk, user=request.user)
    if not deposit_row.provider_reference:
        raise Http404

    payload, signature = provider.paid_event(deposit_row.provider_reference)
    event = provider.verify_webhook(payload=payload, signature=signature)
    handle_event(event, provider_key=str(ProviderKey.FAKE))

    messages.success(request, "Ricarica completata. Le biglie ti aspettano in bacheca.")
    return redirect("payments:deposit_checkout", pk=deposit_row.pk)


# ---------------------------------------------------------------------------
# Withdrawal
# ---------------------------------------------------------------------------


@login_required
def withdraw(request):
    """Ask for money back.

    The page states the PIENA-only rule and the reason for it up front, next to
    the balances that cannot be withdrawn, rather than waiting to refuse after
    the user has typed an amount.
    """
    balances = summary_for(request.user)["balances"]
    form = WithdrawalForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            draft = open_withdrawal(
                user=request.user,
                biglie=form.cleaned_data["biglie"],
                iban=form.cleaned_data["iban"],
            )
        except BiglieError as exc:
            messages.error(request, exc.user_message)
        else:
            return redirect("payments:withdraw_confirm", pk=draft.pk)

    return render(
        request,
        "payments/withdraw.html",
        {
            "form": form,
            "balances": balances,
            "withdrawable": available_piena(request.user),
            "non_withdrawable": [
                (asset, amount)
                for asset, amount in balances.items()
                if not asset.is_withdrawable and amount.minor
            ],
            "explanation": NON_WITHDRAWABLE_EXPLANATION,
            "kyc_tier": current_tier(request.user),
            "auto_threshold": AUTO_APPROVE_MAX_MINOR,
            "withdrawals": Withdrawal.objects.filter(user=request.user)
            .order_by("-created_at")[:5],
        },
    )


@login_required
def withdraw_confirm(request, pk):
    """Confirm a draft, then show its state on the same route.

    POST → confirm → redirect back here as a GET, which then renders the status
    instead of the form. Same URL throughout so an excluded user, who may only
    reach `withdraw` and `withdraw_confirm`, can follow the flow to the end.
    """
    withdrawal = get_object_or_404(Withdrawal, pk=pk, user=request.user)

    if request.method == "POST":
        if withdrawal.state != PaymentState.INITIATED:
            messages.info(request, "Questa richiesta è già stata confermata.")
            return redirect("payments:withdraw_confirm", pk=withdrawal.pk)
        try:
            withdrawal = confirm_withdrawal(withdrawal)
        except BiglieError as exc:
            messages.error(request, exc.user_message)
        else:
            if withdrawal.state == PaymentState.SUCCEEDED:
                messages.success(request, "Prelievo approvato e inviato alla tua banca.")
            else:
                messages.info(
                    request,
                    "Richiesta registrata. Le biglie sono già state messe da parte "
                    "e un operatore la controlla.",
                )
        return redirect("payments:withdraw_confirm", pk=withdrawal.pk)

    return render(
        request,
        "payments/withdraw_confirm.html",
        {
            "withdrawal": withdrawal,
            "is_draft": withdrawal.state == PaymentState.INITIATED,
            "explanation": NON_WITHDRAWABLE_EXPLANATION,
        },
    )


@login_required
def history(request):
    """Every real-money movement on this account, both directions."""
    return render(request, "payments/history.html", recent_movements(request.user))


# ---------------------------------------------------------------------------
# Deliberately absent
# ---------------------------------------------------------------------------
# There is no `cancel_withdrawal` view, and there must not be one. "Annulla il
# prelievo" — offering to put a pending withdrawal back into the playable
# balance — is the highest-converting dark pattern in gambling products, and it
# converts by catching people at the exact moment they decided to stop. A
# reviewer can reject a withdrawal for a compliance reason; the product may not
# ask the user to reconsider. See `payments.services.confirm_withdrawal`.
# ---------------------------------------------------------------------------

"""Responsible-play surfaces.

These pages are reachable from the footer of every page on the site, including
while a user is self-excluded. That placement is not decoration: a control
nobody can find is not a control.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from compliance.constants import LimitKind, LimitPeriod
from compliance.forms import LimitForm, SelfExclusionForm
from compliance.models import KycVerification, PlayerLimit
from compliance.services import (
    active_exclusion,
    cancel_pending_limit_change,
    current_tier,
    end_self_exclusion,
    set_limit,
    start_self_exclusion,
)
from core.exceptions import BiglieError


@login_required
def limits(request):
    """Set deposit, loss, wager and session limits.

    The page states the asymmetry in plain Italian rather than hiding it in
    terms: a user who discovers on submit that their raise takes a day feels
    tricked, and a user who was told up front feels protected.
    """
    if request.method == "POST":
        form = LimitForm(request.POST)
        if form.is_valid():
            limit = set_limit(
                request.user,
                kind=form.cleaned_data["kind"],
                period=form.cleaned_data["period"],
                value=form.cleaned_data["value_minor"],
            )
            if limit.has_pending_change:
                messages.info(
                    request,
                    "Limite più alto registrato: entrerà in vigore fra 24 ore. "
                    "Fino ad allora puoi annullarlo.",
                )
            else:
                messages.success(request, "Limite aggiornato subito.")
            return redirect("compliance:limits")
    else:
        form = LimitForm()

    return render(
        request,
        "compliance/limits.html",
        {
            "form": form,
            "limits": PlayerLimit.objects.filter(user=request.user).order_by("kind", "period"),
            "kinds": LimitKind.choices,
            "periods": LimitPeriod.choices,
        },
    )


@require_POST
@login_required
def cancel_limit_change(request, kind: str, period: str):
    cancel_pending_limit_change(request.user, kind, period)
    messages.success(request, "Aumento annullato.")
    return redirect("compliance:limits")


@login_required
def self_exclusion(request):
    """Start an exclusion. Deliberately a two-step form with no upsell on it."""
    if active_exclusion(request.user) is not None:
        return redirect("compliance:self_exclusion_status")

    if request.method == "POST":
        form = SelfExclusionForm(request.POST)
        if form.is_valid():
            start_self_exclusion(
                request.user,
                until=form.cleaned_data["until"],
                reason=form.cleaned_data.get("reason", ""),
            )
            messages.success(request, "Autoesclusione attivata.")
            return redirect("compliance:self_exclusion_status")
    else:
        form = SelfExclusionForm()

    return render(request, "compliance/self_exclusion.html", {"form": form})


@login_required
def self_exclusion_status(request):
    """Where the middleware sends an excluded user who tries to play.

    Shows the term and what is still available to them — orders, history and
    withdrawing their money, all of which stay open. Being excluded from playing
    must never mean being locked away from your own balance.
    """
    exclusion = active_exclusion(request.user)
    return render(
        request,
        "compliance/self_exclusion_status.html",
        {
            "exclusion": exclusion,
            "remaining": exclusion.remaining() if exclusion else None,
            "can_end": bool(exclusion and exclusion.is_due_to_end()),
            "now": timezone.now(),
        },
    )


@require_POST
@login_required
def end_exclusion(request):
    try:
        end_self_exclusion(request.user)
    except BiglieError as exc:
        messages.warning(request, exc.user_message)
        return redirect("compliance:self_exclusion_status")

    messages.success(request, "Autoesclusione terminata.")
    return redirect("web:dashboard")


@login_required
def kyc(request):
    """Verification status and what it unlocks."""
    return render(
        request,
        "compliance/kyc.html",
        {
            "tier": current_tier(request.user),
            "submissions": KycVerification.objects.filter(user=request.user).order_by(
                "-created_at"
            )[:10],
        },
    )

"""The marketplace's web surface.

Thin on purpose: every one of these views is a permission check, a call into
`marketplace.services` or `salemodes.services`, and a template. No view
computes money and no view knows what a lottery is.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import F
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.exceptions import BiglieError
from marketplace.constants import ListingKind, OrderState
from marketplace.forms import DisputeForm, ListingForm, ShipmentForm
from marketplace.models import Listing, Order
from marketplace.services import (
    announce_draw,
    confirm_delivery,
    create_listing,
    join_sale,
    mark_shipped,
    open_dispute,
    seller_dashboard,
    settle_now,
)
from salemodes import registry
from salemodes import services as sales

# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------


def listing_list(request):
    """The shop window.

    Filters are plain query parameters so a search is a shareable URL. The
    mode filter is built from the registry: a new mechanic appears in the
    sidebar the day it registers, with no change here.
    """
    query = request.GET.get("q", "")
    kind = request.GET.get("kind", "")
    mode = request.GET.get("mode", "")

    listings = Listing.objects.active().select_related("seller").search(query)
    if kind in {k.value for k in ListingKind}:
        listings = listings.of_kind(kind)
    if registry.has(mode):
        listings = listings.filter(sale_mode=mode)

    page = Paginator(listings.order_by("-created_at"), 24).get_page(request.GET.get("page"))

    return render(
        request,
        "marketplace/listing_list.html",
        {
            "page": page,
            "query": query,
            "kind": kind,
            "mode": mode,
            "kinds": list(ListingKind),
            "modes": registry.all_modes(),
        },
    )


def listing_detail(request, pk):
    listing = get_object_or_404(
        Listing.objects.select_related("seller"), pk=pk
    )
    Listing.objects.filter(pk=pk).update(view_count=F("view_count") + 1)

    sale = sales.get_sale(listing.current_sale_id) if listing.current_sale_id else None
    context = {
        "listing": listing,
        "sale": sale,
        "outcome": sales.outcome_for(sale.pk) if sale else None,
    }

    if sale is not None:
        # The mode decides what to render and this view includes it blindly.
        # That indirection is the plugin seam in the presentation layer.
        context["ctx"] = sales.sale_context(sale, user=request.user)
        context["action_url"] = reverse("marketplace:participate", args=[listing.pk])
        context["announce_url"] = reverse("marketplace:announce_draw", args=[listing.pk])
        context["settle_url"] = reverse("marketplace:settle_now", args=[listing.pk])

    return render(request, "marketplace/listing_detail.html", context)


def sale_verify(request, pk):
    """Publish everything needed to re-run a draw.

    A separate page rather than a modal because the point of it is that it can
    be linked to, sent to a sceptical friend, and read by somebody who does not
    have an account.
    """
    sale = sales.get_sale(pk)
    if sale is None:
        raise Http404
    outcome = sales.outcome_for(sale.pk)
    return render(
        request,
        "marketplace/sale_verify.html",
        {"sale": sale, "outcome": outcome,
         "verification": outcome.verification if outcome else {}},
    )


# ---------------------------------------------------------------------------
# Selling
# ---------------------------------------------------------------------------


@login_required
def listing_create(request):
    form = ListingForm(request.POST or None, request.FILES or None)

    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            listing = create_listing(
                seller=request.user,
                title=data["title"],
                description=data["description"],
                kind=data["kind"],
                asset=data["asset"],
                payout_in_biglie=data["payout_in_biglie"],
                sale_mode=data["sale_mode"],
                mode_config=data.get("mode_config", {}),
                image=data.get("image"),
                ships_from=data.get("ships_from", ""),
                shipping_note=data.get("shipping_note", ""),
                digital_payload=data.get("digital_payload", ""),
                bet_question=data.get("bet_question", ""),
                bet_resolution_source=data.get("bet_resolution_source", ""),
                bet_resolves_at=data.get("bet_resolves_at"),
                bet_counter_stake_minor=data.get("bet_counter_stake_minor") or 0,
            )
        except BiglieError as exc:
            messages.error(request, exc.user_message if str(exc) == "" else str(exc))
        else:
            messages.success(request, "Annuncio pubblicato.")
            return redirect(listing.get_absolute_url())

    return render(request, "marketplace/listing_form.html", {"form": form})


@login_required
def seller_dashboard_view(request):
    return render(
        request, "marketplace/seller_dashboard.html", seller_dashboard(request.user)
    )


# ---------------------------------------------------------------------------
# Taking part
# ---------------------------------------------------------------------------


@login_required
@require_POST
def participate(request, pk):
    listing = get_object_or_404(Listing, pk=pk)
    units = int(request.POST.get("units") or 1)
    amount_minor = request.POST.get("amount_minor")

    try:
        result = join_sale(
            listing=listing,
            user=request.user,
            amount_minor=int(amount_minor) if amount_minor else None,
            units=units,
        )
    except BiglieError as exc:
        messages.error(request, str(exc) or exc.user_message)
    else:
        if result.accepted:
            messages.success(request, result.message or "Partecipazione registrata.")
        else:
            messages.warning(request, result.message)

    return HttpResponseRedirect(listing.get_absolute_url())


@login_required
@require_POST
def announce_draw_view(request, pk):
    listing = get_object_or_404(Listing, pk=pk)
    try:
        sale = announce_draw(listing=listing, actor=request.user)
    except BiglieError as exc:
        messages.error(request, str(exc) or exc.user_message)
    else:
        messages.success(
            request,
            f"Estrazione annunciata per le {sale.settlement_due_at:%H:%M}. "
            "Fino ad allora si può ancora partecipare.",
        )
    return HttpResponseRedirect(listing.get_absolute_url())


@login_required
@require_POST
def settle_now_view(request, pk):
    listing = get_object_or_404(Listing, pk=pk)
    try:
        settle_now(listing=listing, actor=request.user)
    except BiglieError as exc:
        messages.error(request, str(exc) or exc.user_message)
    else:
        messages.success(request, "Vendita conclusa.")
    return HttpResponseRedirect(listing.get_absolute_url())


# ---------------------------------------------------------------------------
# Orders — reachable even while self-excluded, by design
# ---------------------------------------------------------------------------


@login_required
def order_list(request):
    orders = (
        Order.objects.for_user(request.user)
        .select_related("listing", "buyer", "seller")
        .order_by("-created_at")
    )
    return render(
        request,
        "marketplace/order_list.html",
        {"orders": orders, "buying": [o for o in orders if o.buyer_id == request.user.pk]},
    )


@login_required
def order_detail(request, pk):
    order = get_object_or_404(
        Order.objects.select_related("listing", "buyer", "seller", "delivery"), pk=pk
    )
    if request.user.pk not in (order.buyer_id, order.seller_id):
        raise Http404

    return render(
        request,
        "marketplace/order_detail.html",
        {
            "order": order,
            "delivery": getattr(order, "delivery", None),
            "is_buyer": request.user.pk == order.buyer_id,
            "shipment_form": ShipmentForm(),
            "dispute_form": DisputeForm(),
            "dispute": order.disputes.order_by("-created_at").first(),
        },
    )


@login_required
@require_POST
def order_ship(request, pk):
    order = get_object_or_404(Order, pk=pk, seller=request.user)
    form = ShipmentForm(request.POST)
    if form.is_valid():
        try:
            mark_shipped(
                order=order,
                carrier=form.cleaned_data["carrier"],
                tracking_code=form.cleaned_data["tracking_code"],
            )
        except BiglieError as exc:
            messages.error(request, str(exc) or exc.user_message)
        else:
            messages.success(request, "Spedizione registrata.")
    return redirect(order.get_absolute_url())


@login_required
@require_POST
def order_confirm(request, pk):
    order = get_object_or_404(Order, pk=pk, buyer=request.user)
    try:
        confirm_delivery(order=order, by_user=request.user)
    except BiglieError as exc:
        messages.error(request, str(exc) or exc.user_message)
    else:
        messages.success(request, "Consegna confermata: il venditore è stato pagato.")
    return redirect(order.get_absolute_url())


@login_required
@require_POST
def order_dispute(request, pk):
    order = get_object_or_404(Order, pk=pk)
    if request.user.pk not in (order.buyer_id, order.seller_id):
        raise Http404

    form = DisputeForm(request.POST)
    if form.is_valid():
        try:
            open_dispute(order=order, user=request.user, reason=form.cleaned_data["reason"])
        except BiglieError as exc:
            messages.error(request, str(exc) or exc.user_message)
        else:
            messages.warning(
                request,
                "Contestazione aperta. L'importo resta bloccato finché non viene decisa.",
            )
    return redirect(order.get_absolute_url())


__all__ = [
    "OrderState",
    "announce_draw_view",
    "listing_create",
    "listing_detail",
    "listing_list",
    "order_confirm",
    "order_detail",
    "order_dispute",
    "order_list",
    "order_ship",
    "participate",
    "sale_verify",
    "seller_dashboard_view",
    "settle_now_view",
]

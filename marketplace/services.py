"""The order engine.

This module is the thing the plugin registry exists to protect. Read it looking
for a branch on which sale mode is running — there isn't one. It knows about
*goods* (a parcel needs escrow, a key does not, a wager resolves later) and
about money, and it learns everything else from `salemodes.services` and from
the events it subscribes to.

Escrow, in one paragraph
------------------------
When a sale settles, the buyer's money is already out of their hands: the sale
context captured it into ESCROW (the seller's share) and HOUSE_REVENUE (the
commission). What this module decides is *when it comes out*. A digital key is
handed over and the seller is paid immediately. A parcel keeps the money in
escrow until the buyer confirms, or until a timeout says the buyer has had long
enough. A dispute clears the timeout, and nothing moves until a human resolves
it. A bet waits for its resolution source.

Every one of those paths ends in either `_pay_seller` or `_refund_buyer`, and
both go through `wallet.services` — sellers are paid into the claim funnel like
everybody else, because a payout that skipped the ritual would be the one piece
of money on the platform that behaves differently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from core.contracts import OrderFulfilled, SaleAborted, SaleSettled
from core.events import bus
from core.exceptions import IllegalStateTransition, SaleModeError
from core.money import Amount, Asset, split_percentage
from identity.services import ensure_seller_profile
from marketplace.constants import (
    BET_RESOLUTION_GRACE,
    ESCROW_AUTO_RELEASE,
    SHIPPING_DEADLINE,
    DisputeState,
    ListingKind,
    ListingState,
    OrderState,
    fee_basis_points,
)
from marketplace.models import Delivery, Dispute, Listing, Order
from salemodes import registry
from salemodes import services as sales
from wallet.constants import AccountKind, CreditSource
from wallet.services import (
    capture_hold,
    credit_pending,
    place_hold,
    recognise_revenue,
    release_hold,
)

logger = logging.getLogger(__name__)
User = get_user_model()


@dataclass(frozen=True, slots=True)
class _HoldRef:
    """A wallet hold referred to by id.

    `wallet.services` re-reads holds under a row lock and only reads `.pk` off
    what it is handed, and `wallet.models` is not importable across the
    boundary. Same seam as `salemodes.holds.HoldRef`; both disappear the day
    the wallet exposes `capture_hold_by_id`.
    """

    pk: UUID


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@transaction.atomic
def create_listing(
    *,
    seller,
    title: str,
    sale_mode: str,
    mode_config: dict[str, Any],
    kind: ListingKind = ListingKind.PHYSICAL,
    description: str = "",
    asset: Asset = Asset.BIGLIA_PIENA,
    payout_in_biglie: bool = True,
    publish: bool = True,
    **extra,
) -> Listing:
    """Create a listing, validating the chosen mechanic's config up front.

    The config goes through `SaleMode.validate_config` here rather than at sale
    time, so a nonsensical lottery (minimum below the ticket price, deadline in
    the past) is a form error the seller can fix, not a sale that opens and can
    never settle.
    """
    if not registry.has(sale_mode):
        raise SaleModeError(f"Modalità di vendita sconosciuta: {sale_mode!r}.")

    kind = ListingKind(kind)
    asset = Asset(asset)

    if not payout_in_biglie and asset is not Asset.BIGLIA_PIENA:
        # Paying out in euros means the proceeds must be withdrawable, and only
        # BIGLIA_PIENA ever is. Silently downgrading the choice would promise
        # the seller a payout the ledger cannot honour.
        raise SaleModeError(
            "Per farti pagare in euro l'annuncio deve essere in biglie piene: "
            "solo quelle tornano denaro."
        )

    if kind is ListingKind.BET:
        _validate_bet(sale_mode, extra)

    config = sales.validate_config(sale_mode, mode_config)
    ensure_seller_profile(seller)

    listing = Listing.objects.create(
        seller=seller,
        title=title,
        description=description,
        kind=kind,
        asset=asset,
        payout_in_biglie=payout_in_biglie,
        sale_mode=sale_mode,
        mode_config=config,
        state=ListingState.DRAFT,
        **{k: v for k, v in extra.items() if hasattr(Listing, k)},
    )

    if publish:
        publish_listing(listing)
    return listing


def _validate_bet(sale_mode: str, extra: dict[str, Any]) -> None:
    """A wager proposition needs a question, a source and somebody covering it."""
    if sale_mode != "fixed":
        raise SaleModeError(
            "Una scommessa si vende a prezzo fisso: la posta è già l'oggetto del gioco."
        )
    if not extra.get("bet_question"):
        raise SaleModeError("Scrivi la proposizione su cui si scommette.")
    if not extra.get("bet_resolution_source"):
        raise SaleModeError(
            "Indica la fonte che deciderà l'esito. Una scommessa senza fonte "
            "dichiarata è una lite annunciata."
        )
    if int(extra.get("bet_counter_stake_minor") or 0) <= 0:
        raise SaleModeError("Devi coprire la scommessa con una posta tua.")


@transaction.atomic
def publish_listing(listing: Listing) -> Listing:
    """Open the sale and put the listing on the shelf."""
    if listing.state == ListingState.ACTIVE:
        return listing

    if listing.listing_kind is ListingKind.BET:
        _cover_bet(listing)

    sale = sales.open_sale(
        listing_id=listing.id,
        seller_id=listing.seller_id,
        mode_key=listing.sale_mode,
        config=listing.mode_config,
        asset=listing.money_asset,
        payout_in_biglie=listing.payout_in_biglie,
        fee_basis_points=fee_basis_points(payout_in_biglie=listing.payout_in_biglie),
    )

    listing.current_sale_id = sale.id
    listing.state = ListingState.ACTIVE
    listing.save(update_fields=["current_sale_id", "state", "updated_at"])
    return listing


def _cover_bet(listing: Listing) -> None:
    """Reserve the seller's side of a wager before anybody may take it."""
    if listing.bet_cover_hold_id:
        return
    hold = place_hold(
        user=listing.seller,
        amount=listing.counter_stake,
        purpose="Copertura di una scommessa",
        reference_type="listing_bet",
        reference_id=str(listing.pk),
    )
    listing.bet_cover_hold_id = hold.pk
    listing.save(update_fields=["bet_cover_hold_id", "updated_at"])


def close_listing(listing: Listing, *, reason: str = "") -> Listing:
    """Withdraw a listing and refund anybody who had joined its sale."""
    sale = sales.get_sale(listing.current_sale_id) if listing.current_sale_id else None
    if sale is not None and not sale.sale_state.is_terminal:
        sales.abort_sale(sale, reason=reason or "Annuncio ritirato dal venditore.")

    if listing.listing_kind is ListingKind.BET and listing.bet_cover_hold_id:
        release_hold(_HoldRef(listing.bet_cover_hold_id))
        listing.bet_cover_hold_id = None

    listing.state = ListingState.CLOSED
    listing.save(update_fields=["state", "bet_cover_hold_id", "updated_at"])
    return listing


# ---------------------------------------------------------------------------
# Buying — a thin pass-through to the mechanic
# ---------------------------------------------------------------------------


def join_sale(*, listing: Listing, user, amount_minor: int | None = None, units: int = 1):
    """Participate in whatever mechanic this listing is being sold under.

    The engine does not compute the price: the mode does, and it refuses the
    participation if what arrives is not what it asked for. That is the seam
    working — a mechanic invented next month sets its own price rules here
    without this function changing.
    """
    sale = sales.get_sale(listing.current_sale_id) if listing.current_sale_id else None
    if sale is None:
        raise SaleModeError("Questo annuncio non è in vendita.")

    amount = Amount(int(amount_minor or 0), listing.money_asset)
    if amount_minor is None:
        amount = _default_amount(sale, units)

    return sales.participate(sale=sale, user=user, amount=amount, units=units)


def _default_amount(sale, units: int) -> Amount:
    """Work out what a participation costs when the form did not say.

    Reads the mode's own published config keys. A mode with an unusual price
    shape simply does not match here and its form sends an explicit amount.
    """
    config = sale.config
    for key in ("ticket_price", "unit_price_minor", "price_minor"):
        if key in config:
            return sale.amount(int(config[key]) * max(1, units))
    return sale.amount(0)


def announce_draw(*, listing: Listing, actor):
    """Seller announces an early draw. Starts the (binding) notice window."""
    sale = sales.get_sale(listing.current_sale_id)
    if sale is None:
        raise SaleModeError("Questo annuncio non è in vendita.")
    return sales.announce_early_settlement(sale=sale, actor_id=actor.pk)


def settle_now(*, listing: Listing, actor):
    """Seller closes the sale early, if the mode's guards allow it right now."""
    sale = sales.get_sale(listing.current_sale_id)
    if sale is None:
        raise SaleModeError("Questo annuncio non è in vendita.")
    return sales.settle_sale(sale, early=True, actor_id=actor.pk)


# ---------------------------------------------------------------------------
# Settlement — the subscriber
# ---------------------------------------------------------------------------


def on_sale_settled(event: SaleSettled) -> Order | None:
    """Turn a settled sale into an order and move the money.

    Runs inside the settlement transaction (`publish_now`), so if anything here
    raises, the sale does not settle either. That is the correct coupling: a
    draw that produced no order would be a prize nobody can collect.
    """
    listing = Listing.objects.filter(pk=event.listing_id).select_for_update().first()
    if listing is None:
        logger.error("SaleSettled for unknown listing %s", event.listing_id)
        return None

    if event.winner_id is None:
        return None

    buyer = User.objects.filter(pk=event.winner_id).first()
    if buyer is None:
        logger.error("SaleSettled for unknown buyer %s", event.winner_id)
        return None

    existing = Order.objects.filter(sale_id=event.sale_id, buyer=buyer).first()
    if existing is not None:
        return existing

    gross = Amount(event.gross_minor, event.asset)
    net, fee = _split_proceeds(event, gross)

    order = Order.objects.create(
        listing=listing,
        sale_id=event.sale_id,
        buyer=buyer,
        seller=listing.seller,
        amount_minor=gross.minor,
        fee_minor=fee.minor,
        net_minor=net.minor,
        asset=gross.asset,
        fee_basis_points=fee_basis_points(payout_in_biglie=event.payout_in_biglie),
        payout_in_biglie=event.payout_in_biglie,
        state=OrderState.ESCROW,
        award_payload=_award_payload(event),
    )
    Delivery.objects.create(order=order)

    _start_escrow(order, listing)

    listing.state = ListingState.SOLD
    listing.save(update_fields=["state", "updated_at"])

    logger.info(
        "order=%s created from sale=%s gross=%s fee=%s net=%s",
        order.pk, event.sale_id, gross.minor, fee.minor, net.minor,
    )
    return order


def _split_proceeds(event: SaleSettled, gross: Amount) -> tuple[Amount, Amount]:
    """Seller's net and the platform's commission, to the scaglia.

    Asks the sale context what it actually captured rather than recomputing the
    percentage: the sale apportioned the commission across many holds and the
    only number that keeps escrow at exactly zero is the one it really took.
    Recomputing would drift by a scaglia per participant, and those scaglie
    would have to come out of somebody's balance.
    """
    award = sales.award_for(event.sale_id, event.winner_id)
    if award is not None:
        return (
            Amount(int(award["net_minor"]), gross.asset),
            Amount(int(award["fee_minor"]), gross.asset),
        )

    rate = fee_basis_points(payout_in_biglie=event.payout_in_biglie)
    fee, net = split_percentage(gross, rate)
    return net, fee


def _award_payload(event: SaleSettled) -> dict[str, Any]:
    award = sales.award_for(event.sale_id, event.winner_id)
    payload = dict(award.get("payload") or {}) if award else {}
    if event.fairness_proof_id:
        payload["fairness_proof_id"] = str(event.fairness_proof_id)
    return payload


def _start_escrow(order: Order, listing: Listing) -> None:
    """Decide when this order's money comes out of escrow, by kind of goods."""
    kind = listing.listing_kind

    if kind is ListingKind.DIGITAL:
        # Delivery is instantaneous and provable. Holding the seller's money
        # for two weeks would protect nobody and cost the seller everything.
        deliver_digital(order)
        return

    if kind is ListingKind.BET:
        deadline = listing.bet_resolves_at or timezone.now()
        order.escrow_release_at = deadline + BET_RESOLUTION_GRACE
    else:
        order.escrow_release_at = timezone.now() + SHIPPING_DEADLINE

    order.save(update_fields=["escrow_release_at", "updated_at"])


def on_sale_aborted(event: SaleAborted) -> None:
    """A sale fell through. Put the listing back on the shelf, or close it.

    `SaleAborted` carries only the sale id, so the listing has to be found
    through a read back into the sale context. Noted in the handover: the event
    should carry `listing_id`.
    """
    sale = sales.get_sale(event.sale_id)
    if sale is None:
        return

    listing = Listing.objects.filter(pk=sale.listing_id).first()
    if listing is None or listing.state == ListingState.SOLD:
        return

    listing.state = ListingState.CLOSED
    listing.current_sale_id = None
    listing.save(update_fields=["state", "current_sale_id", "updated_at"])

    if listing.listing_kind is ListingKind.BET and listing.bet_cover_hold_id:
        release_hold(_HoldRef(listing.bet_cover_hold_id))
        listing.bet_cover_hold_id = None
        listing.save(update_fields=["bet_cover_hold_id", "updated_at"])


# ---------------------------------------------------------------------------
# Delivery and escrow
# ---------------------------------------------------------------------------


@transaction.atomic
def deliver_digital(order: Order) -> Order:
    """Hand over the goods and pay the seller.

    The payload is copied onto the delivery row: editing the listing afterwards
    must not change what the buyer was given.
    """
    delivery = order.delivery
    delivery.payload = order.listing.digital_payload
    delivery.delivered_at = timezone.now()
    delivery.confirmed_at = delivery.delivered_at
    delivery.save(update_fields=["payload", "delivered_at", "confirmed_at", "updated_at"])

    return _pay_seller(order, reason="digital_delivery")


@transaction.atomic
def mark_shipped(*, order: Order, carrier: str = "", tracking_code: str = "") -> Order:
    """Seller says it is on its way. Starts the buyer's confirmation window."""
    if order.order_state not in (OrderState.ESCROW,):
        raise IllegalStateTransition(current=order.state, attempted="ship")

    delivery = order.delivery
    delivery.carrier = carrier[:60]
    delivery.tracking_code = tracking_code[:80]
    delivery.shipped_at = timezone.now()
    delivery.save(update_fields=["carrier", "tracking_code", "shipped_at", "updated_at"])

    order.state = OrderState.SHIPPED
    order.escrow_release_at = timezone.now() + ESCROW_AUTO_RELEASE
    order.save(update_fields=["state", "escrow_release_at", "updated_at"])
    return order


@transaction.atomic
def confirm_delivery(*, order: Order, by_user) -> Order:
    """The buyer says it arrived. Escrow opens immediately.

    Only the buyer may do this. A seller who could confirm their own delivery
    would make escrow decorative.
    """
    if by_user.pk != order.buyer_id:
        raise SaleModeError("Solo chi ha comprato può confermare la consegna.")
    if order.is_frozen:
        raise IllegalStateTransition(current=order.state, attempted="confirm")
    if order.order_state.is_terminal:
        return order

    delivery = order.delivery
    delivery.confirmed_at = timezone.now()
    delivery.delivered_at = delivery.delivered_at or delivery.confirmed_at
    delivery.save(update_fields=["confirmed_at", "delivered_at", "updated_at"])

    return _pay_seller(order, reason="buyer_confirmed")


@transaction.atomic
def open_dispute(*, order: Order, user, reason: str) -> Dispute:
    """Freeze the money and open a case.

    Clearing `escrow_release_at` is the freeze: the sweeper only ever looks at
    orders that have one, so a disputed order cannot time out into anybody's
    pocket while a human is still reading.
    """
    if user.pk not in (order.buyer_id, order.seller_id):
        raise SaleModeError("Solo le parti dell'ordine possono aprire una contestazione.")
    if order.order_state.is_terminal:
        raise IllegalStateTransition(current=order.state, attempted="dispute")

    dispute = Dispute.objects.create(order=order, opened_by=user, reason=reason)

    order.state = OrderState.DISPUTED
    order.escrow_release_at = None
    order.save(update_fields=["state", "escrow_release_at", "updated_at"])

    logger.info("order=%s disputed by=%s", order.pk, user.pk)
    return dispute


@transaction.atomic
def resolve_dispute(*, dispute: Dispute, in_favour_of: str, note: str = "",
                    resolved_by=None) -> Order:
    """Decide a case and unfreeze the money.

    `in_favour_of` is "buyer" or "seller" — there is deliberately no partial
    refund. Splitting escrow needs a ledger primitive the wallet does not
    expose, and a half-refund invented here would be a number nobody could
    reconcile.
    """
    if dispute.state != DisputeState.OPEN:
        raise IllegalStateTransition(current=dispute.state, attempted="resolve")

    order = dispute.order
    if in_favour_of == "buyer":
        dispute.state = DisputeState.RESOLVED_BUYER
        order = _refund_buyer(order, reason="dispute_upheld")
    elif in_favour_of == "seller":
        dispute.state = DisputeState.RESOLVED_SELLER
        order = _pay_seller(order, reason="dispute_rejected", force=True)
    else:
        raise ValueError("in_favour_of must be 'buyer' or 'seller'.")

    dispute.resolution_note = note
    dispute.resolved_at = timezone.now()
    dispute.resolved_by_id = getattr(resolved_by, "pk", None)
    dispute.save(
        update_fields=["state", "resolution_note", "resolved_at", "resolved_by_id", "updated_at"]
    )
    return order


def release_due_escrows(now=None) -> dict[str, int]:
    """Escrow's automatic ending.

    Two timeouts with opposite meanings: a seller who never shipped loses the
    sale, a buyer who never confirmed loses the right to object. Both exist so
    that silence resolves rather than freezes — an escrow with no timeout is
    just a slower way to lose somebody's money.
    """
    now = now or timezone.now()
    paid = refunded = 0

    for order in Order.objects.open_escrows(now).select_related("listing", "delivery"):
        try:
            if order.listing.listing_kind is ListingKind.BET:
                _refund_buyer(order, reason="bet_unresolved")
                _release_bet_cover(order.listing)
                refunded += 1
            elif order.order_state == OrderState.SHIPPED:
                delivery = order.delivery
                delivery.auto_released = True
                delivery.save(update_fields=["auto_released", "updated_at"])
                _pay_seller(order, reason="escrow_timeout")
                paid += 1
            else:
                _refund_buyer(order, reason="never_shipped")
                refunded += 1
        except Exception:
            logger.exception("Failed to resolve escrow for order %s", order.pk)

    return {"paid": paid, "refunded": refunded}


# ---------------------------------------------------------------------------
# Bets
# ---------------------------------------------------------------------------


@transaction.atomic
def resolve_bet(*, listing: Listing, outcome: bool, resolved_by=None) -> list[Order]:
    """Settle a wager listing once its resolution source has spoken.

    Buyer right: they get their stake back plus the seller's cover.
    Buyer wrong: the seller keeps the stake and their cover is released.
    Either way the platform's commission was taken when the sale settled — the
    house does not bet, it charges for holding the money.
    """
    if listing.listing_kind is not ListingKind.BET:
        raise SaleModeError("Questo annuncio non è una scommessa.")
    if listing.bet_outcome is not None:
        raise IllegalStateTransition(current="resolved", attempted="resolve_bet")

    listing.bet_outcome = outcome
    listing.bet_resolved_at = timezone.now()
    listing.save(update_fields=["bet_outcome", "bet_resolved_at", "updated_at"])

    resolved: list[Order] = []
    for order in listing.orders.filter(state__in=(OrderState.ESCROW, OrderState.SHIPPED)):
        if outcome:
            _capture_bet_cover(listing)
            cover = listing.counter_stake
            credit_pending(
                user=order.buyer,
                amount=order.net + cover,
                source=CreditSource.SALE_PROCEEDS,
                idempotency_key=f"bet-win:{order.pk}",
                from_account=AccountKind.ESCROW,
                reference_type="order",
                reference_id=str(order.pk),
            )
            _recognise_fee(order)
            order.state = OrderState.COMPLETED
            order.paid_out_at = timezone.now()
            order.save(update_fields=["state", "paid_out_at", "updated_at"])
            bus.publish(
                OrderFulfilled(order_id=order.id, buyer_id=order.buyer_id,
                               seller_id=order.seller_id)
            )
        else:
            _pay_seller(order, reason="bet_lost")
            _release_bet_cover(listing)
        resolved.append(order)

    return resolved


def _capture_bet_cover(listing: Listing) -> None:
    if not listing.bet_cover_hold_id:
        return
    capture_hold(_HoldRef(listing.bet_cover_hold_id), to=AccountKind.ESCROW)
    listing.bet_cover_hold_id = None
    listing.save(update_fields=["bet_cover_hold_id", "updated_at"])


def _release_bet_cover(listing: Listing) -> None:
    if not listing.bet_cover_hold_id:
        return
    release_hold(_HoldRef(listing.bet_cover_hold_id))
    listing.bet_cover_hold_id = None
    listing.save(update_fields=["bet_cover_hold_id", "updated_at"])


# ---------------------------------------------------------------------------
# Money out
# ---------------------------------------------------------------------------


def _pay_seller(order: Order, *, reason: str, force: bool = False) -> Order:
    """Release escrow to the seller and recognise the commission.

    The seller is paid with `credit_pending`, not into their available balance.
    Sellers go through the claim ritual like everybody else: it is the same
    funnel, the same choice, and the same one place where limits and expiry are
    enforced.
    """
    if order.order_state.is_terminal:
        return order
    if order.is_frozen and not force:
        raise IllegalStateTransition(current=order.state, attempted="pay_seller")

    if order.net.is_positive:
        credit = credit_pending(
            user=order.seller,
            amount=order.net,
            source=CreditSource.SALE_PROCEEDS,
            idempotency_key=f"order-payout:{order.pk}",
            from_account=AccountKind.ESCROW,
            reference_type="order",
            reference_id=str(order.pk),
        )
        order.payout_credit_id = credit.pk

    _recognise_fee(order)

    order.state = OrderState.COMPLETED
    order.paid_out_at = timezone.now()
    order.escrow_release_at = None
    order.save(
        update_fields=["state", "paid_out_at", "escrow_release_at", "payout_credit_id",
                       "updated_at"]
    )

    bus.publish(
        OrderFulfilled(order_id=order.id, buyer_id=order.buyer_id, seller_id=order.seller_id)
    )
    logger.info("order=%s paid seller net=%s reason=%s", order.pk, order.net_minor, reason)
    return order


def _recognise_fee(order: Order) -> None:
    """Book the commission that is already sitting in HOUSE_REVENUE.

    The sale captured the fee holds straight into HOUSE_REVENUE, so this call
    is only about the carve-outs: charity and the redistribution pool come out
    of a balance that exists. Deferring it until the order completes means a
    refunded order never funded a charity figure that has to be walked back.
    """
    if order.fee.is_positive:
        recognise_revenue(amount=order.fee, source="marketplace_fee", reference_id=order.pk)


def _refund_buyer(order: Order, *, reason: str) -> Order:
    """Give the buyer everything back — the seller's share and the commission.

    Two credits because the money is in two places: the principal is in ESCROW
    and the commission is in HOUSE_REVENUE. The platform keeps nothing on a
    sale that did not happen; charging a fee for a parcel that never arrived is
    how a marketplace loses the buyers it has left.
    """
    if order.order_state.is_terminal:
        return order

    if order.net.is_positive:
        credit_pending(
            user=order.buyer,
            amount=order.net,
            source=CreditSource.REFUND,
            idempotency_key=f"order-refund:{order.pk}",
            from_account=AccountKind.ESCROW,
            reference_type="order",
            reference_id=str(order.pk),
        )
    if order.fee.is_positive:
        credit_pending(
            user=order.buyer,
            amount=order.fee,
            source=CreditSource.REFUND,
            idempotency_key=f"order-refund-fee:{order.pk}",
            from_account=AccountKind.HOUSE_REVENUE,
            reference_type="order",
            reference_id=str(order.pk),
        )

    order.state = OrderState.REFUNDED
    order.escrow_release_at = None
    order.save(update_fields=["state", "escrow_release_at", "updated_at"])

    logger.info("order=%s refunded buyer gross=%s reason=%s", order.pk, order.amount_minor, reason)
    return order


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def orders_for(user, *, limit: int = 100) -> list[Order]:
    return list(
        Order.objects.for_user(user)
        .select_related("listing", "buyer", "seller", "delivery")
        .order_by("-created_at")[:limit]
    )


def seller_dashboard(seller) -> dict[str, Any]:
    """Everything a seller's shop page needs, in one place."""
    listings = list(
        Listing.objects.filter(seller=seller).order_by("-created_at")[:100]
    )
    sales_made = list(
        Order.objects.filter(seller=seller)
        .select_related("listing", "buyer")
        .order_by("-created_at")[:50]
    )

    live = []
    for listing in listings:
        sale = sales.get_sale(listing.current_sale_id) if listing.current_sale_id else None
        if sale is not None and not sale.sale_state.is_terminal:
            live.append(sales.sale_context(sale, user=seller))

    return {
        "listings": listings,
        "orders": sales_made,
        "live_sales": live,
        "awaiting_shipment": [o for o in sales_made if o.order_state == OrderState.ESCROW],
        "disputed": [o for o in sales_made if o.order_state == OrderState.DISPUTED],
    }

"""Goods escrow: the part of a marketplace that decides who eats a bad trade.

Escrow is the only reason a stranger will send money for an object they cannot
see. Its whole value is in the edges — what happens when the seller never ships,
when the buyer never confirms, when someone objects — because the happy path
would work without it.

Two timeouts with opposite meanings run this: a seller who never ships loses the
sale, a buyer who never confirms loses the right to object. Both exist so that
silence *resolves* rather than freezes. An escrow with no timeout is just a
slower way to lose somebody's money.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.exceptions import BiglieError
from core.money import Amount, Asset
from marketplace import services as market
from marketplace.constants import ListingKind, OrderState
from marketplace.models import Order
from salemodes import services as sales
from wallet.constants import AccountKind, EntryKind
from wallet.ledger import Leg, post, reconcile, system_account, user_account
from wallet.models import PendingCredit

PIENA = Asset.BIGLIA_PIENA


def biglie(units: str) -> Amount:
    return Amount.from_major(units, PIENA)


def make_user(email: str):
    return get_user_model().objects.create_user(
        email=email, password="x" * 12,
        date_of_birth=timezone.localdate().replace(year=1990),
    )


def fund(user, amount: Amount) -> None:
    post(
        kind=EntryKind.ADJUSTMENT,
        legs=[
            Leg(system_account(AccountKind.ISSUANCE, amount.asset), -amount),
            Leg(user_account(user, AccountKind.USER_AVAILABLE, amount.asset), amount),
        ],
        description="test fixture funding",
    )


def available(user) -> Amount:
    return user_account(user, AccountKind.USER_AVAILABLE, PIENA).balance


@pytest.fixture
def seller(db):
    return make_user("negozio@example.com")


@pytest.fixture
def buyer(db):
    person = make_user("compratore@example.com")
    fund(person, biglie("500"))
    return person


def sell_and_buy(seller, buyer, callbacks, *, kind=ListingKind.PHYSICAL, **extra) -> Order:
    """A fixed-price listing bought outright — the simplest path to an order."""
    listing = market.create_listing(
        seller=seller,
        title="Oggetto",
        sale_mode="fixed",
        mode_config={"price_minor": biglie("100").minor},
        kind=kind,
        **extra,
    )
    with callbacks(execute=True):
        market.join_sale(listing=listing, user=buyer)

    sale = sales.get_sale(listing.current_sale_id)
    if sale is not None and not sale.sale_state.is_terminal:
        with callbacks(execute=True):
            sales.settle_sale(sale)

    return Order.objects.get(listing=listing)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_the_seller_is_not_paid_before_the_buyer_confirms(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    """The entire point. Paying on purchase would make escrow decorative."""
    order = sell_and_buy(seller, buyer, django_capture_on_commit_callbacks)

    assert order.order_state == OrderState.ESCROW
    assert not PendingCredit.objects.filter(user=seller).exists()


def test_confirming_delivery_pays_the_seller(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    order = sell_and_buy(seller, buyer, django_capture_on_commit_callbacks)

    with django_capture_on_commit_callbacks(execute=True):
        market.confirm_delivery(order=order, by_user=buyer)

    assert PendingCredit.objects.filter(user=seller).exists()
    assert reconcile(record_anomalies=False) == []


def test_a_seller_cannot_confirm_their_own_delivery(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    """Otherwise escrow is a formality the seller can dismiss."""
    order = sell_and_buy(seller, buyer, django_capture_on_commit_callbacks)

    with pytest.raises(BiglieError):
        market.confirm_delivery(order=order, by_user=seller)

    assert not PendingCredit.objects.filter(user=seller).exists()


def test_a_digital_item_pays_immediately(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    """Delivery is instantaneous and provable, so holding the seller's money
    for two weeks would protect nobody and cost the seller everything."""
    order = sell_and_buy(
        seller, buyer, django_capture_on_commit_callbacks,
        kind=ListingKind.DIGITAL, digital_payload="CHIAVE-XYZ",
    )

    assert order.order_state != OrderState.ESCROW
    assert PendingCredit.objects.filter(user=seller).exists()


# ---------------------------------------------------------------------------
# The edges
# ---------------------------------------------------------------------------


def test_a_dispute_freezes_the_money(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    """Neither side gets paid while a human is still deciding."""
    order = sell_and_buy(seller, buyer, django_capture_on_commit_callbacks)

    market.open_dispute(order=order, user=buyer, reason="Mai arrivato")
    order.refresh_from_db()

    assert order.order_state == OrderState.DISPUTED
    assert not PendingCredit.objects.filter(user=seller).exists()


def test_a_frozen_order_does_not_auto_release(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    """The timeout must not quietly pay the seller out from under an open
    dispute — that would make objecting pointless."""
    order = sell_and_buy(seller, buyer, django_capture_on_commit_callbacks)
    market.open_dispute(order=order, user=buyer, reason="Non corrisponde")

    order.refresh_from_db()
    order.escrow_release_at = timezone.now() - timedelta(days=1)
    order.save(update_fields=["escrow_release_at"])

    with django_capture_on_commit_callbacks(execute=True):
        market.release_due_escrows()

    assert not PendingCredit.objects.filter(user=seller).exists()


def test_a_dispute_resolved_for_the_buyer_refunds_them(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    order = sell_and_buy(seller, buyer, django_capture_on_commit_callbacks)
    dispute = market.open_dispute(order=order, user=buyer, reason="Rotto")

    with django_capture_on_commit_callbacks(execute=True):
        market.resolve_dispute(dispute=dispute, in_favour_of="buyer", note="Foto chiare")

    assert PendingCredit.objects.filter(user=buyer).exists()
    assert not PendingCredit.objects.filter(user=seller).exists()
    assert reconcile(record_anomalies=False) == []


def test_a_dispute_resolved_for_the_seller_pays_them(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    order = sell_and_buy(seller, buyer, django_capture_on_commit_callbacks)
    dispute = market.open_dispute(order=order, user=buyer, reason="Ripensamento")

    with django_capture_on_commit_callbacks(execute=True):
        market.resolve_dispute(dispute=dispute, in_favour_of="seller", note="Tracking consegnato")

    assert PendingCredit.objects.filter(user=seller).exists()
    assert reconcile(record_anomalies=False) == []


def test_a_silent_buyer_loses_the_right_to_object(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    """The seller shipped; the buyer never confirmed. Escrow times out in the
    seller's favour, because a buyer who goes quiet must not be able to strand
    somebody else's money indefinitely."""
    order = sell_and_buy(seller, buyer, django_capture_on_commit_callbacks)
    market.mark_shipped(order=order, carrier="Poste", tracking_code="ABC123")

    order.refresh_from_db()
    order.escrow_release_at = timezone.now() - timedelta(days=1)
    order.save(update_fields=["escrow_release_at"])

    with django_capture_on_commit_callbacks(execute=True):
        result = market.release_due_escrows()

    assert result["paid"] == 1
    assert PendingCredit.objects.filter(user=seller).exists()
    assert reconcile(record_anomalies=False) == []


def test_a_seller_who_never_ships_loses_the_sale(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    """The same timeout, the opposite outcome. Never marking the order shipped
    is not a way to hold a buyer's money hostage — silence resolves against
    whichever side went quiet."""
    order = sell_and_buy(seller, buyer, django_capture_on_commit_callbacks)
    order.escrow_release_at = timezone.now() - timedelta(days=1)
    order.save(update_fields=["escrow_release_at"])

    with django_capture_on_commit_callbacks(execute=True):
        result = market.release_due_escrows()

    assert result["refunded"] == 1
    assert PendingCredit.objects.filter(user=buyer).exists()
    assert not PendingCredit.objects.filter(user=seller).exists()
    assert reconcile(record_anomalies=False) == []


def test_money_is_conserved_across_every_outcome(
    db, seller, buyer, django_capture_on_commit_callbacks
):
    """Whichever way a disputed sale falls, the books still close."""
    order = sell_and_buy(seller, buyer, django_capture_on_commit_callbacks)
    dispute = market.open_dispute(order=order, user=buyer, reason="Contestato")

    with django_capture_on_commit_callbacks(execute=True):
        market.resolve_dispute(dispute=dispute, in_favour_of="buyer", note="")

    assert reconcile(record_anomalies=False) == []

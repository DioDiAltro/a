"""Putting money in — the only inflationary path in the platform.

The property under test throughout: biglie appear when, and only when, a signed
provider event says euros arrived. Everything before that point creates rows,
not money.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from compliance.constants import LimitKind, LimitPeriod
from compliance.services import set_limit
from core.exceptions import InvalidAmount, LimitExceeded, SelfExcluded
from core.money import Amount, Asset
from payments.constants import MAX_DEPOSIT_MINOR, MIN_DEPOSIT_MINOR, PaymentState
from payments.models import Deposit
from payments.services import biglie_for_euro, confirm_deposit, start_deposit
from payments.tests.conftest import PIENA, biglie, euro
from wallet.ledger import circulating_supply, reconcile
from wallet.models import PendingCredit

# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_a_euro_buys_a_biglia(db):
    """Parity is deliberate: nobody should need mental arithmetic to know the stake."""
    assert biglie_for_euro(euro("12.34")) == biglie("12.34")


def test_deposits_are_priced_in_euros_only(db):
    with pytest.raises(InvalidAmount):
        biglie_for_euro(biglie("10"))


# ---------------------------------------------------------------------------
# Starting a deposit moves nothing
# ---------------------------------------------------------------------------


def test_starting_a_deposit_creates_an_intent_and_mints_nothing(db, alice, provider):
    deposit = start_deposit(user=alice, euro=euro("25"))

    assert deposit.state == PaymentState.PENDING
    assert deposit.provider_reference.startswith("pi_fake_")
    assert deposit.client_secret
    assert deposit.biglie == biglie("25")

    # Nothing has been issued: the user has not paid yet.
    assert circulating_supply(PIENA).minor == 0
    assert not PendingCredit.objects.filter(user=alice).exists()


def test_the_intent_is_idempotent_per_deposit(db, alice, provider):
    """A retried create must not leave two live intents for one deposit row."""
    deposit = start_deposit(user=alice, euro=euro("25"))
    same = provider.create_deposit_intent(
        amount=euro("25"),
        reference=str(deposit.id),
        idempotency_key=f"deposit-intent:{deposit.id}",
    )
    assert same.reference == deposit.provider_reference


@pytest.mark.parametrize(
    "amount_minor",
    [MIN_DEPOSIT_MINOR - 1, MAX_DEPOSIT_MINOR + 1],
)
def test_amounts_outside_the_bounds_are_refused(db, alice, amount_minor):
    with pytest.raises(InvalidAmount):
        start_deposit(user=alice, euro=Amount(amount_minor, Asset.EUR))

    assert Deposit.objects.count() == 0


def test_a_self_excluded_user_cannot_deposit(db, alice):
    """Money out stays open while excluded. Money in does not."""
    alice.is_self_excluded = True
    alice.save(update_fields=["is_self_excluded"])

    with pytest.raises(SelfExcluded):
        start_deposit(user=alice, euro=euro("25"))


def test_the_users_own_deposit_limit_is_enforced_before_the_payment_sheet(db, alice):
    set_limit(alice, LimitKind.DEPOSIT, LimitPeriod.DAY, 5_000)

    with pytest.raises(LimitExceeded):
        start_deposit(user=alice, euro=euro("60"))

    assert Deposit.objects.count() == 0


# ---------------------------------------------------------------------------
# Confirmation is the mint
# ---------------------------------------------------------------------------


def test_confirmation_issues_biglie_as_a_pending_credit(db, alice, provider):
    """Even a top-up goes through the claim ritual — there is no bypass."""
    deposit = confirm_deposit(deposit=start_deposit(user=alice, euro=euro("40")))

    assert deposit.state == PaymentState.SUCCEEDED
    assert deposit.confirmed_at is not None
    assert circulating_supply(PIENA) == biglie("40")

    credit = PendingCredit.objects.get(user=alice)
    assert credit.amount == biglie("40")
    assert reconcile(record_anomalies=False) == []


def test_confirming_twice_is_a_no_op(db, alice, provider):
    """Belt and braces behind the webhook's own idempotency."""
    deposit = start_deposit(user=alice, euro=euro("40"))
    confirm_deposit(deposit=deposit)
    confirm_deposit(deposit=deposit)

    assert circulating_supply(PIENA) == biglie("40")
    assert PendingCredit.objects.filter(user=alice).count() == 1


def test_a_settled_deposit_counts_against_the_deposit_limit(db, alice, provider):
    """Recorded after settlement, so a declined card never eats the allowance."""
    from compliance.services import usage_in_window

    deposit = start_deposit(user=alice, euro=euro("40"))
    assert usage_in_window(alice, LimitKind.DEPOSIT, LimitPeriod.DAY) == 0

    confirm_deposit(deposit=deposit)
    assert usage_in_window(alice, LimitKind.DEPOSIT, LimitPeriod.DAY) == 4_000


# ---------------------------------------------------------------------------
# Through the interface
# ---------------------------------------------------------------------------


def test_the_deposit_screens_walk_end_to_end(client, alice, provider):
    """Form → intent → signed webhook → credit, with no network anywhere."""
    client.force_login(alice)

    posted = client.post(reverse("payments:deposit"), {"euro": "25.00"})
    assert posted.status_code == 302

    deposit = Deposit.objects.get(user=alice)
    assert posted.url == reverse("payments:deposit_checkout", args=[deposit.pk])

    checkout = client.get(posted.url)
    assert checkout.status_code == 200
    assert "Simula il pagamento" in checkout.content.decode()

    # The dev button produces a genuinely signed event and pushes it through
    # the same handler the provider's callback uses.
    client.post(reverse("payments:deposit_simulate", args=[deposit.pk]))

    deposit.refresh_from_db()
    assert deposit.state == PaymentState.SUCCEEDED
    assert circulating_supply(PIENA) == biglie("25")


def test_the_simulate_route_is_unreachable_in_live_mode(client, alice, provider, settings):
    """A route that fabricates a settled payment is a mint if it ever ships."""
    client.force_login(alice)
    deposit = start_deposit(user=alice, euro=euro("25"))

    settings.LIVE_MODE = True
    settings.STRIPE_SECRET_KEY = "sk_test_x"  # so `get_provider` returns Stripe

    response = client.post(reverse("payments:deposit_simulate", args=[deposit.pk]))

    assert response.status_code == 404
    assert circulating_supply(PIENA).minor == 0


def test_one_user_cannot_see_another_users_deposit(client, alice, provider, db):
    from django.contrib.auth import get_user_model

    deposit = start_deposit(user=alice, euro=euro("25"))
    mallory = get_user_model().objects.create_user(
        email="mallory@example.com", password="x" * 12
    )
    client.force_login(mallory)

    assert client.get(
        reverse("payments:deposit_checkout", args=[deposit.pk])
    ).status_code == 404

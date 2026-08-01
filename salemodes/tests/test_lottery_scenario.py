"""Example 1 from the brief, end to end.

    «Un venditore mette in vendita un oggetto e decide di venderlo tramite una
    lotteria. Decide che serviranno almeno 30 biglie e 10 giorni. Non c'è un
    massimo. Superato il minimo può estrarre quando vuole, e scegliere se
    incassare in euro o in biglie.»

Two kinds of assertion here, and the second is the one that matters. The first
is that the mechanic works. The second is that **the money adds up** — that at
the end of a settled lottery the sum of every account is exactly what it was
before, with value moved rather than created. A lottery that draws correctly
while leaking a scaglia per ticket is still broken.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.contracts import CHARITY_BASIS_POINTS
from core.exceptions import BiglieError
from core.money import Amount, Asset, split_percentage
from marketplace import services as market
from marketplace.models import Order
from salemodes import services as sales
from salemodes.constants import SaleState
from wallet.constants import AccountKind, EntryKind
from wallet.ledger import Leg, circulating_supply, post, reconcile, system_account, user_account
from wallet.models import PendingCredit

PIENA = Asset.BIGLIA_PIENA

TICKET_PRICE = 500          # 5 biglie a ticket
MINIMUM = 3_000             # 30 biglie, as the brief says
DEADLINE_DAYS = 10


def biglie(units: str) -> Amount:
    return Amount.from_major(units, PIENA)


def make_user(email: str):
    return get_user_model().objects.create_user(
        email=email, password="x" * 12,
        date_of_birth=timezone.localdate().replace(year=1990),
    )


def fund(user, amount: Amount) -> None:
    """Establish a starting balance without exercising the claim funnel."""
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


def held(user) -> Amount:
    return user_account(user, AccountKind.USER_HELD, PIENA).balance


@pytest.fixture
def seller(db):
    return make_user("venditore@example.com")


@pytest.fixture
def players(db):
    people = [make_user(f"giocatore{i}@example.com") for i in range(4)]
    for person in people:
        fund(person, biglie("100"))
    return people


@pytest.fixture
def lottery(seller):
    """The listing exactly as the brief describes it."""
    return market.create_listing(
        seller=seller,
        title="Una chitarra",
        sale_mode="lottery",
        mode_config={
            "ticket_price": TICKET_PRICE,
            "minimum_minor": MINIMUM,
            "deadline_days": DEADLINE_DAYS,
            "early_draw_allowed": True,
            "payout_in_biglie": True,
        },
    )


def buy(listing, user, tickets: int = 1):
    return market.join_sale(listing=listing, user=user, units=tickets)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_the_brief_scenario_end_to_end(db, lottery, seller, players, django_capture_on_commit_callbacks):
    """Minimum 30 / 10 days, pushed past the minimum, drawn early, paid out."""
    sale = sales.get_sale(lottery.current_sale_id)
    assert sale.state == SaleState.OPEN
    assert sale.closes_at - sale.opens_at == timedelta(days=DEADLINE_DAYS)

    # Four tickets: 20 biglie, still short of the 30 minimum.
    with django_capture_on_commit_callbacks(execute=True):
        for player in players:
            buy(lottery, player)

    sale.refresh_from_db()
    assert sale.total_minor == 4 * TICKET_PRICE
    assert sale.state == SaleState.OPEN, "must not be drawable below the minimum"

    # There is no maximum, so people keep buying. Two more tickets take it to 30.
    with django_capture_on_commit_callbacks(execute=True):
        buy(lottery, players[0])
        buy(lottery, players[1])

    sale.refresh_from_db()
    assert sale.total_minor == MINIMUM
    assert sale.state == SaleState.THRESHOLD_MET

    # Past the minimum it keeps accepting — 40 biglie in total, as the brief has it.
    with django_capture_on_commit_callbacks(execute=True):
        buy(lottery, players[2], tickets=2)

    sale.refresh_from_db()
    assert sale.total_minor == 4_000

    # Now the seller may draw early — after announcing and waiting.
    sales.announce_early_settlement(sale=sale, actor_id=seller.pk)
    sale.refresh_from_db()
    sale.settlement_due_at = timezone.now() - timedelta(seconds=1)
    sale.save(update_fields=["settlement_due_at"])

    with django_capture_on_commit_callbacks(execute=True):
        outcome = sales.settle_sale(sale, early=True, actor_id=seller.pk)

    sale.refresh_from_db()
    assert sale.state == SaleState.SETTLED
    assert sale.winner_id is not None
    assert outcome.winner_id == sale.winner_id

    # The winner is somebody who actually bought a ticket.
    assert sale.winner_id in {p.pk for p in players}

    # The order engine produced the sale.
    order = Order.objects.filter(listing=lottery).first()
    assert order is not None
    assert order.buyer_id == sale.winner_id
    assert order.seller_id == seller.pk

    # The seller is NOT paid yet, and that is correct: a physical item's
    # proceeds sit in escrow until the buyer confirms it arrived. Paying the
    # seller the moment a draw resolves would make escrow decorative.
    assert not PendingCredit.objects.filter(user=seller).exists()

    winner = next(p for p in players if p.pk == sale.winner_id)
    with django_capture_on_commit_callbacks(execute=True):
        market.confirm_delivery(order=order, by_user=winner)

    # Now the seller is paid — through the claim funnel, like everybody else.
    assert PendingCredit.objects.filter(user=seller).exists(), "the seller was never credited"

    assert reconcile(record_anomalies=False) == []


def test_the_pot_is_conserved(db, lottery, seller, players, django_capture_on_commit_callbacks):
    """Nothing is created, nothing evaporates.

    The strongest single assertion in this file: total circulating supply is
    identical before and after a lottery settles. Tickets moved from six
    players to one seller and one winner, minus a commission that went to the
    house — but not one scaglia was minted or lost on the way.
    """
    supply_before = circulating_supply(PIENA)

    with django_capture_on_commit_callbacks(execute=True):
        for player in players:
            buy(lottery, player, tickets=2)

    sale = sales.get_sale(lottery.current_sale_id)
    sales.announce_early_settlement(sale=sale, actor_id=seller.pk)
    sale.refresh_from_db()
    sale.settlement_due_at = timezone.now() - timedelta(seconds=1)
    sale.save(update_fields=["settlement_due_at"])

    with django_capture_on_commit_callbacks(execute=True):
        sales.settle_sale(sale, early=True, actor_id=seller.pk)

    assert circulating_supply(PIENA) == supply_before
    assert reconcile(record_anomalies=False) == []


def test_the_charity_cut_is_taken_from_the_commission(
    db, lottery, seller, players, django_capture_on_commit_callbacks
):
    """3% of the house's cut, in the same transaction, provable from the ledger."""
    charity_before = system_account(AccountKind.CHARITY_POOL, PIENA).balance

    with django_capture_on_commit_callbacks(execute=True):
        for player in players:
            buy(lottery, player, tickets=2)

    sale = sales.get_sale(lottery.current_sale_id)
    sales.announce_early_settlement(sale=sale, actor_id=seller.pk)
    sale.refresh_from_db()
    sale.settlement_due_at = timezone.now() - timedelta(seconds=1)
    sale.save(update_fields=["settlement_due_at"])

    with django_capture_on_commit_callbacks(execute=True):
        sales.settle_sale(sale, early=True, actor_id=seller.pk)

    # The commission is recognised when the seller is actually paid, not when
    # the draw resolves — so the charity figure follows real revenue rather
    # than revenue we expect to earn once the item ships.
    sale.refresh_from_db()
    order = Order.objects.get(listing=lottery)
    winner = next(p for p in players if p.pk == sale.winner_id)
    with django_capture_on_commit_callbacks(execute=True):
        market.confirm_delivery(order=order, by_user=winner)

    charity_after = system_account(AccountKind.CHARITY_POOL, PIENA).balance
    assert charity_after > charity_before, "no charity allocation was made"

    # The cut is a share of the commission, not of turnover. On a 40-biglia pot
    # the platform earns a commission and gives away 3% of *that* — see
    # docs/ECONOMY.md for why 3% of turnover would exceed the margin.
    pot = sale.total
    commission, _ = split_percentage(pot, sale.fee_basis_points)
    expected_max, _ = split_percentage(commission, CHARITY_BASIS_POINTS)
    assert (charity_after - charity_before) <= expected_max + Amount(1, PIENA)


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_below_the_minimum_at_the_deadline_everyone_is_refunded(
    db, lottery, seller, players, django_capture_on_commit_callbacks
):
    """To the exact scaglia. A refund that is 'about right' is a theft."""
    before = {p.pk: available(p) for p in players}

    with django_capture_on_commit_callbacks(execute=True):
        buy(lottery, players[0])
        buy(lottery, players[1])

    sale = sales.get_sale(lottery.current_sale_id)
    assert sale.total_minor < MINIMUM

    sale.closes_at = timezone.now() - timedelta(seconds=1)
    sale.save(update_fields=["closes_at"])

    with django_capture_on_commit_callbacks(execute=True):
        sales.settle_due()

    sale.refresh_from_db()
    assert sale.state == SaleState.ABORTED

    for player in players:
        assert available(player) == before[player.pk], "refund was not exact"
        assert held(player) == Amount.zero(PIENA), "money left frozen after abort"

    assert reconcile(record_anomalies=False) == []


def test_a_seller_cannot_buy_tickets_in_their_own_lottery(db, lottery, seller):
    """Otherwise the seller inflates their own pot and can win their own item —
    the cheapest way to fake a busy marketplace."""
    fund(seller, biglie("100"))

    with pytest.raises(BiglieError):
        buy(lottery, seller)


def test_the_draw_is_refused_before_the_minimum(
    db, lottery, seller, players, django_capture_on_commit_callbacks
):
    """Below the threshold the participants were promised a deadline, and an
    early draw would be the seller changing the rules after seeing the list."""
    with django_capture_on_commit_callbacks(execute=True):
        buy(lottery, players[0])

    sale = sales.get_sale(lottery.current_sale_id)

    with pytest.raises(BiglieError):
        sales.announce_early_settlement(sale=sale, actor_id=seller.pk)

    with pytest.raises(BiglieError):
        sales.settle_sale(sale, early=True, actor_id=seller.pk)


def test_the_draw_is_refused_during_the_notice_window(
    db, lottery, seller, players, django_capture_on_commit_callbacks
):
    """The notice exists so a seller cannot pick the instant that suits a
    confederate. Announcing and drawing in the same breath defeats it."""
    with django_capture_on_commit_callbacks(execute=True):
        for player in players:
            buy(lottery, player, tickets=2)

    sale = sales.get_sale(lottery.current_sale_id)
    sales.announce_early_settlement(sale=sale, actor_id=seller.pk)
    sale.refresh_from_db()

    with pytest.raises(BiglieError):
        sales.settle_sale(sale, early=True, actor_id=seller.pk)


def test_re_announcing_does_not_push_the_window_out(
    db, lottery, seller, players, django_capture_on_commit_callbacks
):
    """A seller who could re-announce could shop for a favourable moment one
    window at a time, which is the same abuse the notice was meant to stop."""
    with django_capture_on_commit_callbacks(execute=True):
        for player in players:
            buy(lottery, player, tickets=2)

    sale = sales.get_sale(lottery.current_sale_id)
    sales.announce_early_settlement(sale=sale, actor_id=seller.pk)
    sale.refresh_from_db()
    first_due = sale.settlement_due_at

    sales.announce_early_settlement(sale=sale, actor_id=seller.pk)
    sale.refresh_from_db()

    assert sale.settlement_due_at == first_due


def test_only_the_seller_can_draw(db, lottery, seller, players, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        for player in players:
            buy(lottery, player, tickets=2)

    sale = sales.get_sale(lottery.current_sale_id)

    with pytest.raises(BiglieError):
        sales.announce_early_settlement(sale=sale, actor_id=players[0].pk)


def test_settling_twice_pays_nobody_twice(
    db, lottery, seller, players, django_capture_on_commit_callbacks
):
    """A retried request, a double-clicked button, a Celery task that ran twice."""
    with django_capture_on_commit_callbacks(execute=True):
        for player in players:
            buy(lottery, player, tickets=2)

    sale = sales.get_sale(lottery.current_sale_id)
    sales.announce_early_settlement(sale=sale, actor_id=seller.pk)
    sale.refresh_from_db()
    sale.settlement_due_at = timezone.now() - timedelta(seconds=1)
    sale.save(update_fields=["settlement_due_at"])

    with django_capture_on_commit_callbacks(execute=True):
        first = sales.settle_sale(sale, early=True, actor_id=seller.pk)

    with django_capture_on_commit_callbacks(execute=True):
        second = sales.settle_sale(sale, early=True, actor_id=seller.pk)

    assert first.pk == second.pk
    assert Order.objects.filter(listing=lottery).count() == 1
    assert reconcile(record_anomalies=False) == []

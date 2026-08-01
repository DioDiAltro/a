"""The architecture test for the sale-mode registry.

The brief asked for endless inventiveness in *how* things are sold. That is only
achievable if a new mechanic can be added without touching the order engine —
otherwise every new idea is a change to the code that moves money, and the rate
of new ideas is capped by how brave you feel about that code.

This file proves the seam is real by inventing a mechanic that did not exist
when the engine was written, registering it from inside the test, and driving it
end to end through the ordinary public path: `create_listing`, `join_sale`,
`settle_now`. Nothing in `marketplace` or `salemodes.services` knows the mode
exists. If any of it needed to, this test would not compile.

The mechanic chosen is a sealed-bid auction, picked because it breaks the
assumptions the existing modes share: there is no published price, no threshold,
participants cannot see each other's bids, and the winner pays what they offered
rather than what the seller asked.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.exceptions import BiglieError, SaleModeError
from core.money import Amount, Asset
from marketplace import services as market
from marketplace.models import Order
from salemodes import holds, registry
from salemodes import services as sales
from salemodes.base import (
    Award,
    Decision,
    ParticipationResult,
    SaleMode,
    SettlementResult,
)
from salemodes.constants import SaleState
from wallet.constants import AccountKind, EntryKind
from wallet.ledger import Leg, post, reconcile, system_account, user_account

PIENA = Asset.BIGLIA_PIENA


def biglie(units: str) -> Amount:
    return Amount.from_major(units, PIENA)


# ---------------------------------------------------------------------------
# A mechanic the engine has never heard of
# ---------------------------------------------------------------------------


class SealedBid(SaleMode):
    """Everyone bids blind; the highest bid wins and pays what they offered.

    Deliberately unlike every registered mode: no published price, no minimum,
    no early settlement, and losers are refunded in full rather than having
    contributed to a pot.
    """

    key = "sealedbid_test"
    label = "Offerta segreta"
    description = "Ognuno offre alla cieca. Vince l'offerta più alta."
    requires_deadline = True
    allows_seller_participation = False
    allows_repeat_participation = False

    def validate_config(self, config):
        return {"reserve_minor": int(config.get("reserve_minor", 0))}

    def initial_closes_at(self, config, opens_at):
        return opens_at + timedelta(days=3)

    def on_participation(self, sale, user, amount, units):
        reserve = int(sale.config.get("reserve_minor", 0))
        if amount.minor < reserve:
            return ParticipationResult.refused("L'offerta è sotto la riserva.")

        # The mode places the hold, through the same published helper every
        # other mode uses. That the engine does not do this for us is the
        # reason a mode can price itself however it likes.
        participation = holds.record_participation(sale, user, amount, units=units)
        return ParticipationResult(
            accepted=True, participation=participation, message="Offerta registrata."
        )

    def can_settle_early(self, sale):
        return Decision.no("Le offerte segrete si aprono solo alla scadenza.")

    def settle(self, sale):
        # Earliest bid wins a tie — the tiebreak has to be something a bidder
        # cannot manipulate after the fact, and "who committed first" is that.
        bids = list(sale.participations.active().order_by("-amount_minor", "created_at"))
        if not bids:
            return SettlementResult.abort("Nessuna offerta.")

        best, losers = bids[0], bids[1:]

        # The mode decides who pays and who gets their money back. The engine
        # captures nothing on its own, which is precisely why a mechanic with
        # an outcome nobody anticipated can still settle correctly.
        holds.capture(best)
        for loser in losers:
            holds.release(loser, reason="offerta non vincente")

        return SettlementResult(
            awards=(Award(user_id=best.user_id, gross=best.amount),),
            payload={"bid_count": len(bids)},
        )


@pytest.fixture
def sealed_bid_mode():
    """Register for the duration of one test, then leave the registry as found."""
    registry.register(SealedBid)
    yield registry.get(SealedBid.key)
    registry.unregister(SealedBid.key)


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------


def make_user(email: str):
    return get_user_model().objects.create_user(
        email=email, password="x" * 12, date_of_birth=timezone.localdate().replace(year=1990)
    )


def fund(user, amount: Amount) -> None:
    """Put spendable biglie in a user's account, exactly.

    Posts straight to the ledger rather than going through deposit-and-claim.
    A fixture should establish state, not exercise a second feature: routing
    through `claim_safe` would take the 1% fee and every balance assertion
    below would be about the claim fee rather than about the sale.
    """
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
    return make_user("seller@example.com")


@pytest.fixture
def bidders(db):
    people = [make_user(f"bidder{i}@example.com") for i in range(3)]
    for person in people:
        fund(person, biglie("200"))
    return people


# ---------------------------------------------------------------------------
# The proof
# ---------------------------------------------------------------------------


def test_a_mode_invented_today_runs_on_an_engine_written_before_it(
    db, sealed_bid_mode, seller, bidders, django_capture_on_commit_callbacks
):
    """The whole point of the registry, asserted end to end.

    Note what this test does NOT do: it does not import the sealed-bid mode
    anywhere in `marketplace` or `salemodes.services`, does not add a branch to
    the order engine, and does not special-case settlement. The mechanic is
    supplied entirely by the class above.
    """
    listing = market.create_listing(
        seller=seller,
        title="Un disco raro",
        sale_mode=SealedBid.key,
        mode_config={"reserve_minor": biglie("10").minor},
    )

    low, mid, high = bidders
    with django_capture_on_commit_callbacks(execute=True):
        market.join_sale(listing=listing, user=low, amount_minor=biglie("20").minor)
        market.join_sale(listing=listing, user=mid, amount_minor=biglie("55").minor)
        market.join_sale(listing=listing, user=high, amount_minor=biglie("90").minor)

    sale = sales.get_sale(listing.current_sale_id)
    assert sale.participations.active().count() == 3

    # Bids are held, not spent: every bidder is down exactly their offer while
    # the sale is open, and nobody has lost anything yet.
    assert available(low) == biglie("180")
    assert available(mid) == biglie("145")
    assert available(high) == biglie("110")

    # The deadline arrives. `settle_due` is generic — it asks each sale's own
    # mode what to do and does not know a sealed bid from a lottery.
    sale.closes_at = timezone.now() - timedelta(seconds=1)
    sale.save(update_fields=["closes_at"])

    with django_capture_on_commit_callbacks(execute=True):
        outcomes = sales.settle_due()

    assert len(outcomes) == 1
    sale.refresh_from_db()
    assert sale.state == SaleState.SETTLED
    assert sale.winner_id == high.pk

    # The losers get everything back, to the exact scaglia.
    assert available(low) == biglie("200")
    assert available(mid) == biglie("200")

    # And the ledger is still honest after a mechanic it was never told about.
    assert reconcile(record_anomalies=False) == []


def test_the_order_engine_produces_an_order_for_an_unknown_mode(
    db, sealed_bid_mode, seller, bidders, django_capture_on_commit_callbacks
):
    """`marketplace` reacts to `SaleSettled`, not to a mode it recognises.

    This is the half of the seam that matters commercially: a new mechanic must
    also *sell things*, not merely resolve. If the order engine had a lookup
    table of known modes, this would return None.
    """
    listing = market.create_listing(
        seller=seller, title="Una stampa numerata",
        sale_mode=SealedBid.key, mode_config={},
    )

    winner = bidders[0]
    with django_capture_on_commit_callbacks(execute=True):
        market.join_sale(listing=listing, user=winner, amount_minor=biglie("40").minor)

    sale = sales.get_sale(listing.current_sale_id)
    sale.closes_at = timezone.now() - timedelta(seconds=1)
    sale.save(update_fields=["closes_at"])

    with django_capture_on_commit_callbacks(execute=True):
        sales.settle_due()

    order = Order.objects.filter(listing=listing).first()
    assert order is not None, "the order engine ignored a mode it did not recognise"
    assert order.buyer_id == winner.pk
    assert order.seller_id == seller.pk


def test_the_mode_sets_its_own_rules_without_engine_support(
    db, sealed_bid_mode, seller, bidders
):
    """The engine enforces nothing about price; the mode does.

    A reserve price is a concept no other registered mode has. It works because
    `on_participation` is the mode's, and `join_sale` simply relays the refusal.
    """
    listing = market.create_listing(
        seller=seller, title="Con riserva",
        sale_mode=SealedBid.key, mode_config={"reserve_minor": biglie("50").minor},
    )

    result = market.join_sale(
        listing=listing, user=bidders[0], amount_minor=biglie("30").minor
    )
    assert not result.accepted
    assert "riserva" in result.message.lower()


def test_a_seller_cannot_bid_on_their_own_sealed_bid(db, sealed_bid_mode, seller, bidders):
    """`allows_seller_participation = False` is honoured by the engine.

    The flag is declared on the mode and enforced in `salemodes.services`, so
    every mode gets self-dealing protection by declaring one attribute rather
    than by remembering to write the check.
    """
    fund(seller, biglie("100"))
    listing = market.create_listing(
        seller=seller, title="Autogol", sale_mode=SealedBid.key, mode_config={},
    )

    with pytest.raises(BiglieError):
        market.join_sale(listing=listing, user=seller, amount_minor=biglie("20").minor)


def test_registering_a_duplicate_key_is_refused(db, sealed_bid_mode):
    """Two modes answering to one key would make behaviour depend on import
    order, which is not a design decision anybody wants to be making."""

    class Impostor(SaleMode):
        key = SealedBid.key
        label = "Impostore"

        def validate_config(self, config):
            return {}

        def on_participation(self, sale, user, amount, units):
            return ParticipationResult(accepted=True)

        def settle(self, sale):
            return SettlementResult.abort("no")

    with pytest.raises(SaleModeError):
        registry.register(Impostor)


def test_the_registry_returns_to_its_prior_state(db):
    """The fixture cleans up after itself — otherwise one test's invention
    would leak into the mode list every other test and page reads."""
    assert not registry.has(SealedBid.key)

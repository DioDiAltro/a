"""The 3%, end to end.

Two properties matter here and nothing else really does:

1. What the public page calls "messo da parte" is the ledger's CHARITY_POOL
   balance, not a counter that happens to be near it.
2. A distribution empties what it distributes, exactly. Charity money that
   evaporates into a rounding remainder is the worst possible bug on this
   platform — small enough never to be noticed, and indefensible once it is.
"""

from __future__ import annotations

import pytest

from charity.constants import PayoutState
from charity.models import Beneficiary, CharityAllocation, CharityPayout
from charity.selectors import transparency_summary
from charity.services import (
    committed_minor,
    confirm_payout,
    distributable,
    distribute_pool,
    fail_payout,
    total_raised,
)
from core.contracts import CHARITY_BASIS_POINTS
from core.money import Amount, Asset, allocate, split_percentage
from wallet.constants import AccountKind
from wallet.ledger import reconcile, system_account
from wallet.selectors import charity_pool_balances
from wallet.services import recognise_revenue

VINTA = Asset.BIGLIA_VINTA
PIENA = Asset.BIGLIA_PIENA


def biglie(units: str, asset: Asset = VINTA) -> Amount:
    return Amount.from_major(units, asset)


@pytest.fixture
def beneficiaries(db):
    """Deliberately awkward weights: 5/3/1 divides almost nothing evenly."""
    return [
        Beneficiary.objects.create(name="Croce Blu", weight=5),
        Beneficiary.objects.create(name="Mensa Solidale", weight=3),
        Beneficiary.objects.create(name="Biblioteca di Quartiere", weight=1),
    ]


def fund_pool(*amounts: Amount) -> Amount:
    """Recognise revenue and return what charity should have received."""
    expected = Amount.zero(amounts[0].asset)
    for amount in amounts:
        recognise_revenue(amount=amount, source="test")
        cut, _ = split_percentage(amount, CHARITY_BASIS_POINTS)
        expected = expected + cut
    return expected


# ---------------------------------------------------------------------------
# The carve-out ties to the ledger
# ---------------------------------------------------------------------------


def test_carve_out_matches_the_ledger_after_a_series_of_revenue_events(db):
    """Several fees, awkward sizes, one pool balance.

    The amounts are chosen so the 3% does not divide cleanly: 3% of 3333
    scaglie is 99.99, and `split_percentage` rounds the cut down. Summing the
    per-event expectation rather than taking 3% of the total is the point —
    it proves the carve-out is per-transaction, which is what makes the figure
    on the public page a ledger balance rather than an estimate.
    """
    expected = fund_pool(
        biglie("33.33"), biglie("1000"), biglie("0.07"), biglie("777.77"), biglie("12")
    )

    assert system_account(AccountKind.CHARITY_POOL, VINTA).balance == expected
    assert charity_pool_balances()[VINTA] == expected
    assert reconcile(record_anomalies=False) == []


def test_the_running_record_matches_the_pool(db, django_capture_on_commit_callbacks):
    """`CharityAllocated` is what feeds the public total; it must not drift."""
    with django_capture_on_commit_callbacks(execute=True):
        expected = fund_pool(biglie("500"), biglie("250"), biglie("33.33"))

    assert total_raised()[VINTA] == expected
    assert charity_pool_balances()[VINTA] == expected


def test_a_redelivered_allocation_event_does_not_inflate_the_total(db):
    """The bus is in-process today and a broker is the stated future."""
    from charity.handlers import on_charity_allocated
    from core.contracts import CharityAllocated

    event = CharityAllocated(
        amount_minor=1_234, asset=VINTA, basis_points=CHARITY_BASIS_POINTS, beneficiary_id=None
    )
    on_charity_allocated(event)
    on_charity_allocated(event)

    assert CharityAllocation.objects.count() == 1
    assert total_raised()[VINTA].minor == 1_234


# ---------------------------------------------------------------------------
# Distribution loses nothing
# ---------------------------------------------------------------------------


def test_distribution_empties_the_pool_exactly(db, beneficiaries):
    """Sum of the shares == the pool, to the scaglia."""
    fund_pool(biglie("33333.37"))
    available = distributable(VINTA)
    assert available.is_positive

    payouts = distribute_pool(asset=VINTA)

    assert len(payouts) == 3
    assert sum(p.amount_minor for p in payouts) == available.minor
    assert distributable(VINTA) == Amount.zero(VINTA)


def test_shares_follow_the_weights(db, beneficiaries):
    fund_pool(biglie("10000"))
    available = distributable(VINTA)

    payouts = {p.beneficiary.name: p.amount_minor for p in distribute_pool(asset=VINTA)}
    expected = allocate(available, [5, 3, 1])

    assert payouts["Croce Blu"] == expected[0].minor
    assert payouts["Mensa Solidale"] == expected[1].minor
    assert payouts["Biblioteca di Quartiere"] == expected[2].minor


@pytest.mark.parametrize("minor", [1_001, 1_003, 9_999, 100_000, 123_457])
def test_no_minor_unit_is_ever_lost_or_invented(db, beneficiaries, minor):
    """Property test over sizes that cannot divide evenly by 5/3/1."""
    shares = allocate(Amount(minor, VINTA), [5, 3, 1])

    assert sum(s.minor for s in shares) == minor
    assert all(s.minor >= 0 for s in shares)


def test_a_second_run_does_not_promise_the_same_money_twice(db, beneficiaries):
    """Instructed payouts are spoken for even before the transfer is made."""
    fund_pool(biglie("5000"))
    first = distribute_pool(asset=VINTA)
    assert first

    second = distribute_pool(asset=VINTA)

    assert second == []
    assert committed_minor(VINTA) == sum(p.amount_minor for p in first)


def test_new_revenue_after_a_distribution_is_distributable_again(db, beneficiaries):
    fund_pool(biglie("5000"))
    distribute_pool(asset=VINTA)

    expected = fund_pool(biglie("2000"))

    assert distributable(VINTA) == expected


def test_a_failed_transfer_returns_the_money_to_the_pool(db, beneficiaries):
    fund_pool(biglie("5000"))
    payouts = distribute_pool(asset=VINTA)
    bounced = payouts[0]

    fail_payout(bounced, reason="IBAN errato")

    assert distributable(VINTA).minor == bounced.amount_minor


def test_dust_is_left_in_the_pool_rather_than_wired(db, beneficiaries):
    """A bank transfer of four scaglie costs more than it gives."""
    fund_pool(biglie("1"))  # 3% of 100 scaglie = 3 scaglie

    assert distribute_pool(asset=VINTA) == []
    assert distributable(VINTA).minor == 3


def test_an_empty_beneficiary_list_leaves_the_money_alone(db):
    """No beneficiary configured must never mean 'the platform keeps it'."""
    expected = fund_pool(biglie("5000"))

    assert distribute_pool(asset=VINTA) == []
    assert charity_pool_balances()[VINTA] == expected


def test_inactive_beneficiaries_receive_nothing(db, beneficiaries):
    beneficiaries[2].is_active = False
    beneficiaries[2].save(update_fields=["is_active"])
    fund_pool(biglie("5000"))
    available = distributable(VINTA)

    payouts = distribute_pool(asset=VINTA)

    assert {p.beneficiary.name for p in payouts} == {"Croce Blu", "Mensa Solidale"}
    assert sum(p.amount_minor for p in payouts) == available.minor


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def test_confirming_a_payout_requires_a_transfer_reference(db, beneficiaries):
    from core.exceptions import InvalidAmount

    fund_pool(biglie("5000"))
    payout = distribute_pool(asset=VINTA)[0]

    with pytest.raises(InvalidAmount):
        confirm_payout(payout, reference="   ")

    confirm_payout(payout, reference="BONIFICO-2026-0001")
    payout.refresh_from_db()
    assert payout.state == PayoutState.PAID
    assert payout.paid_at is not None


def test_a_paid_payout_cannot_be_confirmed_again(db, beneficiaries):
    from core.exceptions import IllegalStateTransition

    fund_pool(biglie("5000"))
    payout = distribute_pool(asset=VINTA)[0]
    confirm_payout(payout, reference="BONIFICO-1")

    with pytest.raises(IllegalStateTransition):
        confirm_payout(payout, reference="BONIFICO-1-bis")


# ---------------------------------------------------------------------------
# The public page
# ---------------------------------------------------------------------------


def test_transparency_summary_separates_raised_pooled_and_paid(
    db, beneficiaries, django_capture_on_commit_callbacks
):
    """Three different numbers. Collapsing them is how "raccolto" becomes a lie."""
    with django_capture_on_commit_callbacks(execute=True):
        raised = fund_pool(biglie("10000"))

    payout = distribute_pool(asset=VINTA)[0]
    confirm_payout(payout, reference="BONIFICO-1")

    summary = transparency_summary(VINTA)

    assert summary["raised"] == raised
    assert summary["in_pool"] == raised  # the ledger still holds it until wired
    assert summary["paid"].minor == payout.amount_minor
    assert summary["awaiting_transfer"].minor == raised.minor - payout.amount_minor
    assert len(summary["beneficiaries"]) == 3


def test_transparency_page_renders_without_login(client, db, beneficiaries):
    from django.urls import reverse

    fund_pool(biglie("1000"))
    distribute_pool(asset=VINTA)

    response = client.get(reverse("charity:transparency"))

    assert response.status_code == 200
    assert "Croce Blu" in response.content.decode()


def test_beneficiary_page_renders(client, db, beneficiaries):
    from django.urls import reverse

    response = client.get(
        reverse("charity:beneficiary", kwargs={"slug": beneficiaries[0].slug})
    )

    assert response.status_code == 200


def test_payouts_are_denominated_in_the_asset_that_was_earned(db, beneficiaries):
    """Charity is paid in the flavour the fee was taken in, never converted."""
    fund_pool(biglie("5000", PIENA))

    payouts = distribute_pool(asset=PIENA)

    assert payouts
    assert {p.asset for p in payouts} == {PIENA}
    assert CharityPayout.objects.filter(asset=str(VINTA)).count() == 0

"""Ledger invariants.

These tests are the safety net for every euro on the platform. They are
deliberately about *arithmetic and refusal*, not about features: if one of them
fails, no feature is worth shipping.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from core.exceptions import InsufficientFunds, LedgerImbalance
from core.money import Amount, Asset, allocate, split_percentage
from wallet.constants import AccountKind, EntryKind
from wallet.ledger import Leg, circulating_supply, post, reconcile, system_account, user_account

PIENA = Asset.BIGLIA_PIENA


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(email="a@example.com", password="x" * 12)


@pytest.fixture
def other(db):
    return get_user_model().objects.create_user(email="b@example.com", password="x" * 12)


def biglie(units: str) -> Amount:
    return Amount.from_major(units, PIENA)


# ---------------------------------------------------------------------------
# Money value object
# ---------------------------------------------------------------------------


def test_amount_refuses_floats():
    """The one input that would quietly corrupt every downstream figure."""
    with pytest.raises(Exception):
        Amount.from_major(10.5, PIENA)


def test_assets_never_mix():
    with pytest.raises(Exception):
        Amount(100, PIENA) + Amount(100, Asset.EUR)


def test_split_percentage_loses_nothing():
    """The property the charity split and the house rake both depend on."""
    for minor in range(0, 1000, 7):
        total = Amount(minor, PIENA)
        cut, rest = split_percentage(total, 300)
        assert cut + rest == total, f"{minor} leaked a minor unit"
        assert cut.minor >= 0 and rest.minor >= 0


def test_allocate_distributes_every_minor_unit():
    """Prize tiers must hand out exactly the pot — not a scaglia more or less."""
    pot = Amount(10_007, PIENA)
    shares = allocate(pot, [50, 30, 20])
    assert sum(s.minor for s in shares) == pot.minor


# ---------------------------------------------------------------------------
# Posting engine
# ---------------------------------------------------------------------------


def test_unbalanced_entry_is_refused(db, user):
    """The invariant. If this ever passes, the books can lie."""
    with pytest.raises(LedgerImbalance):
        post(
            kind=EntryKind.ADJUSTMENT,
            legs=[
                Leg(user_account(user, AccountKind.USER_AVAILABLE, PIENA), biglie("10")),
                Leg(system_account(AccountKind.ISSUANCE, PIENA), -biglie("9")),
            ],
        )


def test_entry_balanced_per_asset_not_across_assets(db, user):
    """€5 must not be allowed to cancel out 5 biglie."""
    with pytest.raises(LedgerImbalance):
        post(
            kind=EntryKind.DEPOSIT,
            legs=[
                Leg(system_account(AccountKind.EXTERNAL_FIAT, Asset.EUR),
                    -Amount.from_major("5", Asset.EUR)),
                Leg(user_account(user, AccountKind.USER_AVAILABLE, PIENA), biglie("5")),
            ],
        )


def test_user_cannot_go_negative(db, user):
    with pytest.raises(InsufficientFunds):
        post(
            kind=EntryKind.PURCHASE,
            legs=[
                Leg(user_account(user, AccountKind.USER_AVAILABLE, PIENA), -biglie("10")),
                Leg(system_account(AccountKind.ESCROW, PIENA), biglie("10")),
            ],
        )


def test_system_accounts_may_go_negative(db, user):
    """Issuance going negative is not a bug — it is the record of supply."""
    post(
        kind=EntryKind.DEPOSIT,
        legs=[
            Leg(system_account(AccountKind.ISSUANCE, PIENA), -biglie("100")),
            Leg(user_account(user, AccountKind.USER_AVAILABLE, PIENA), biglie("100")),
        ],
    )
    assert circulating_supply(PIENA) == biglie("100")


def test_failed_entry_moves_nothing(db, user):
    """Rollback is all-or-nothing: a refused entry leaves no trace."""
    account = user_account(user, AccountKind.USER_AVAILABLE, PIENA)
    post(
        kind=EntryKind.DEPOSIT,
        legs=[
            Leg(system_account(AccountKind.ISSUANCE, PIENA), -biglie("50")),
            Leg(account, biglie("50")),
        ],
    )

    with pytest.raises(InsufficientFunds):
        post(
            kind=EntryKind.PURCHASE,
            legs=[
                Leg(account, -biglie("80")),
                Leg(system_account(AccountKind.ESCROW, PIENA), biglie("80")),
            ],
        )

    account.refresh_from_db()
    assert account.balance == biglie("50")


def test_balances_match_postings(db, user, other):
    """Reconciliation: the cache agrees with the truth."""
    post(
        kind=EntryKind.DEPOSIT,
        legs=[
            Leg(system_account(AccountKind.ISSUANCE, PIENA), -biglie("30")),
            Leg(user_account(user, AccountKind.USER_AVAILABLE, PIENA), biglie("30")),
        ],
    )
    post(
        kind=EntryKind.GIFT,
        legs=[
            Leg(user_account(user, AccountKind.USER_AVAILABLE, PIENA), -biglie("12")),
            Leg(user_account(other, AccountKind.USER_AVAILABLE, PIENA), biglie("12")),
        ],
    )

    assert reconcile(record_anomalies=False) == []
    assert user_account(user, AccountKind.USER_AVAILABLE, PIENA).balance == biglie("18")
    assert user_account(other, AccountKind.USER_AVAILABLE, PIENA).balance == biglie("12")


def test_repeated_account_in_one_entry_nets_correctly(db, user):
    """A single entry that both debits and credits an account must net, not
    evaluate each leg against the balance independently."""
    account = user_account(user, AccountKind.USER_AVAILABLE, PIENA)
    post(
        kind=EntryKind.ADJUSTMENT,
        legs=[
            Leg(system_account(AccountKind.ISSUANCE, PIENA), -biglie("10")),
            Leg(account, biglie("40")),
            Leg(account, -biglie("30")),
        ],
    )
    account.refresh_from_db()
    assert account.balance == biglie("10")
    assert reconcile(record_anomalies=False) == []

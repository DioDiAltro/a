"""Fixtures shared by the games tests.

These reach into `wallet.ledger` and `identity.services` directly, which
production code in `games` may not do. That is fine and deliberate: test files
are excluded from `tests/test_boundaries.py`, and a fixture that set up a
balance by going through six service calls would be testing the fixture.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.money import Amount, Asset
from games.engine import Outcome, get_game
from identity.services import award_xp, ensure_seller_profile, register_user
from wallet.constants import AccountKind, CreditSource, EntryKind
from wallet.ledger import Leg, post, system_account, user_account
from wallet.services import credit_pending

VINTA = Asset.BIGLIA_VINTA
PIENA = Asset.BIGLIA_PIENA


def _make_user(email: str):
    return register_user(
        email=email,
        password="correct-horse-battery",
        date_of_birth=date(1990, 5, 17),
        display_name=email.split("@")[0],
    )


@pytest.fixture
def player(db):
    return _make_user("player@example.com")


@pytest.fixture
def rival(db):
    return _make_user("rival@example.com")


@pytest.fixture
def fund():
    """Put spendable balance in a user's available account.

    Straight to the ledger: the point of the fixture is that the money is there,
    not how it got there.
    """

    def _fund(user, amount: Amount):
        post(
            kind=EntryKind.DEPOSIT,
            legs=[
                Leg(system_account(AccountKind.ISSUANCE, amount.asset), -amount),
                Leg(user_account(user, AccountKind.USER_AVAILABLE, amount.asset), amount),
            ],
            description="test funding",
        )

    return _fund


@pytest.fixture
def pending_credit():
    """A claimable credit, which is the only thing a claim gamble can consume."""
    counter = {"n": 0}

    def _credit(user, amount: Amount):
        counter["n"] += 1
        return credit_pending(
            user=user,
            amount=amount,
            source=CreditSource.GAME_WIN,
            idempotency_key=f"test-credit:{user.pk}:{counter['n']}",
        )

    return _credit


@pytest.fixture
def seller():
    """Give a user a shop and enough XP to be allowed to duel at all."""

    def _seller(user, *, accepts_duels: bool = True, xp: int = 400):
        profile = ensure_seller_profile(user, shop_name=f"Bottega {user.handle}")
        profile.accepts_duels = accepts_duels
        profile.save(update_fields=["accepts_duels"])
        award_xp(user, xp, reason="test")
        return profile

    return _seller


@pytest.fixture
def force_outcome(monkeypatch):
    """Pin a game's derivation so a test can be about settlement, not luck.

    Patches the registered instance's `derive`, so verification inside the same
    test still replays through the patched function and stays consistent.
    """

    def _force(game_key: str, multiplier_bp: int, *, detail: dict | None = None):
        definition = get_game(game_key)
        outcome = Outcome(
            game_key=str(definition.key),
            slot_key="forced",
            label="esito forzato",
            multiplier_bp=multiplier_bp,
            detail=detail if detail is not None else {"forced": True},
        )
        monkeypatch.setattr(definition, "derive", lambda rng: outcome)
        return outcome

    return _force


def issuance_minor(asset: Asset = VINTA) -> int:
    """Magnitude of the issuance account: biglie in circulation."""
    return -system_account(AccountKind.ISSUANCE, asset).balance_minor


def house_minor(asset: Asset = VINTA) -> int:
    return system_account(AccountKind.HOUSE_REVENUE, asset).balance_minor


def available_minor(user, asset: Asset = VINTA) -> int:
    return user_account(user, AccountKind.USER_AVAILABLE, asset).balance_minor


def held_minor(user, asset: Asset = VINTA) -> int:
    return user_account(user, AccountKind.USER_HELD, asset).balance_minor

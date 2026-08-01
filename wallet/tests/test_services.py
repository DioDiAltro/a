"""Wallet service behaviour.

These cover the rules that the rest of the platform is allowed to assume:
deposits are the only mint, claims happen once, gifts change asset, revenue
carves out charity synchronously, and the drip cannot conjure money.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from core.contracts import CHARITY_BASIS_POINTS
from core.exceptions import AssetNotTransferable, IllegalStateTransition, InvalidAmount
from core.money import Amount, Asset, split_percentage
from wallet.constants import AccountKind, CreditSource, CreditState
from wallet.ledger import circulating_supply, reconcile, system_account, user_account
from wallet.models import PendingCredit
from wallet.services import (
    burn_for_withdrawal,
    claim_safe,
    credit_pending,
    drip,
    expire_credit,
    gift,
    issue_for_deposit,
    recognise_revenue,
    settle_risked_claim,
)

PIENA = Asset.BIGLIA_PIENA
VINTA = Asset.BIGLIA_VINTA
DONO = Asset.BIGLIA_DONO


def biglie(units: str, asset: Asset = PIENA) -> Amount:
    return Amount.from_major(units, asset)


def euro(units: str) -> Amount:
    return Amount.from_major(units, Asset.EUR)


@pytest.fixture
def alice(db):
    return get_user_model().objects.create_user(email="alice@example.com", password="x" * 12)


@pytest.fixture
def bob(db):
    return get_user_model().objects.create_user(email="bob@example.com", password="x" * 12)


@pytest.fixture
def funded(alice):
    """Alice with 100 claimed, spendable biglie."""
    credit = issue_for_deposit(
        user=alice, euro=euro("100"), biglie=biglie("100"),
        idempotency_key="dep-1", reference_id="dep-1",
    )
    claim_safe(credit=credit, idempotency_key="claim-dep-1")
    return alice


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def test_deposit_is_the_only_mint(db, alice):
    issue_for_deposit(
        user=alice, euro=euro("50"), biglie=biglie("50"),
        idempotency_key="dep-x", reference_id="dep-x",
    )
    assert circulating_supply(PIENA) == biglie("50")
    assert reconcile(record_anomalies=False) == []


def test_deposit_lands_as_pending_not_spendable(db, alice):
    """Even a top-up goes through the claim ritual — there is no bypass."""
    issue_for_deposit(
        user=alice, euro=euro("20"), biglie=biglie("20"),
        idempotency_key="dep-y", reference_id="dep-y",
    )
    available = user_account(alice, AccountKind.USER_AVAILABLE, PIENA)
    pending = user_account(alice, AccountKind.USER_PENDING, PIENA)
    assert available.balance == biglie("0")
    assert pending.balance == biglie("20")


def test_replayed_deposit_credits_once(db, alice):
    """A retried payment webhook must not double the user's money."""
    first = issue_for_deposit(
        user=alice, euro=euro("30"), biglie=biglie("30"),
        idempotency_key="dep-dup", reference_id="dep-dup",
    )
    second = issue_for_deposit(
        user=alice, euro=euro("30"), biglie=biglie("30"),
        idempotency_key="dep-dup", reference_id="dep-dup",
    )
    assert first.pk == second.pk
    assert circulating_supply(PIENA) == biglie("30")
    assert PendingCredit.objects.filter(user=alice).count() == 1


def test_only_piena_can_be_withdrawn(db, alice):
    with pytest.raises(InvalidAmount):
        burn_for_withdrawal(
            user=alice, biglie=biglie("10", VINTA), euro=euro("10"),
            idempotency_key="wd-bad",
        )


def test_withdrawal_burns_supply(db, funded):
    burn_for_withdrawal(
        user=funded, biglie=biglie("40"), euro=euro("40"), idempotency_key="wd-1",
    )
    assert circulating_supply(PIENA) == biglie("60")
    assert reconcile(record_anomalies=False) == []


# ---------------------------------------------------------------------------
# The claim funnel
# ---------------------------------------------------------------------------


def test_safe_claim_takes_the_fee_and_nothing_else(db, alice):
    credit = credit_pending(
        user=alice, amount=biglie("100", VINTA), source=CreditSource.GAME_WIN,
        idempotency_key="c-1", reference_id="c-1",
    )
    settled = claim_safe(credit=credit, idempotency_key="claim-1")

    fee, net = split_percentage(biglie("100", VINTA), 100)  # 1%
    assert settled == net
    assert user_account(alice, AccountKind.USER_AVAILABLE, VINTA).balance == net
    assert reconcile(record_anomalies=False) == []


def test_a_credit_cannot_be_claimed_twice(db, alice):
    credit = credit_pending(
        user=alice, amount=biglie("10", VINTA), source=CreditSource.GIFT,
        idempotency_key="c-2", reference_id="c-2",
    )
    claim_safe(credit=credit, idempotency_key="claim-2a")

    with pytest.raises(IllegalStateTransition):
        claim_safe(credit=credit, idempotency_key="claim-2b")


def test_expired_credit_cannot_be_claimed(db, alice):
    credit = credit_pending(
        user=alice, amount=biglie("10", VINTA), source=CreditSource.DRIP,
        idempotency_key="c-3", reference_id="c-3",
    )
    credit.expires_at = timezone.now() - timedelta(seconds=1)
    credit.save(update_fields=["expires_at"])

    with pytest.raises(IllegalStateTransition):
        claim_safe(credit=credit, idempotency_key="claim-3")


def test_losing_a_gamble_burns_the_credit_and_pays_nothing(db, alice):
    credit = credit_pending(
        user=alice, amount=biglie("20", VINTA), source=CreditSource.GAME_WIN,
        idempotency_key="c-4", reference_id="c-4",
    )
    settled = settle_risked_claim(
        credit=credit, payout=Amount.zero(VINTA),
        game_round_id=None, idempotency_key="risk-4",
    )

    credit.refresh_from_db()
    assert settled == Amount.zero(VINTA)
    assert credit.state == CreditState.BURNED
    assert user_account(alice, AccountKind.USER_AVAILABLE, VINTA).balance == Amount.zero(VINTA)
    assert reconcile(record_anomalies=False) == []


def test_winning_a_gamble_is_paid_from_the_house_not_minted(db, alice):
    """The invariant that makes the whole economy survivable."""
    supply_before = circulating_supply(VINTA)

    credit = credit_pending(
        user=alice, amount=biglie("20", VINTA), source=CreditSource.GAME_WIN,
        idempotency_key="c-5", reference_id="c-5",
    )
    settle_risked_claim(
        credit=credit, payout=biglie("60", VINTA),
        game_round_id=None, idempotency_key="risk-5",
    )

    assert user_account(alice, AccountKind.USER_AVAILABLE, VINTA).balance == biglie("60", VINTA)
    # The user is 60 up, but not one new biglia exists: the house bankroll paid.
    assert circulating_supply(VINTA) == supply_before
    assert system_account(AccountKind.HOUSE_REVENUE, VINTA).balance.minor < 0
    assert reconcile(record_anomalies=False) == []


def test_expired_credit_funds_the_redistribution_pool(db, alice):
    credit = credit_pending(
        user=alice, amount=biglie("15", VINTA), source=CreditSource.DRIP,
        idempotency_key="c-6", reference_id="c-6",
    )
    credit.expires_at = timezone.now() - timedelta(days=1)
    credit.save(update_fields=["expires_at"])

    expire_credit(credit)
    credit.refresh_from_db()

    assert credit.state == CreditState.EXPIRED
    assert system_account(AccountKind.REDISTRIBUTION_POOL, VINTA).balance == biglie("15", VINTA)


# ---------------------------------------------------------------------------
# Gifts
# ---------------------------------------------------------------------------


def test_gift_arrives_as_dono_not_as_piena(db, funded, bob):
    """The anti-laundering line: what you send is not what they can withdraw."""
    credit = gift(sender=funded, recipient=bob, amount=biglie("25"), idempotency_key="g-1")

    assert credit.amount.asset is DONO
    assert not credit.amount.asset.is_withdrawable
    assert credit.state == CreditState.PENDING  # still has to be claimed
    assert reconcile(record_anomalies=False) == []


def test_winnings_cannot_be_gifted(db, alice, bob):
    credit = credit_pending(
        user=alice, amount=biglie("30", VINTA), source=CreditSource.GAME_WIN,
        idempotency_key="c-7", reference_id="c-7",
    )
    claim_safe(credit=credit, idempotency_key="claim-7")

    with pytest.raises(AssetNotTransferable):
        gift(sender=alice, recipient=bob, amount=biglie("10", VINTA), idempotency_key="g-2")


def test_cannot_gift_to_self(db, funded):
    with pytest.raises(InvalidAmount):
        gift(sender=funded, recipient=funded, amount=biglie("5"), idempotency_key="g-3")


# ---------------------------------------------------------------------------
# Revenue, charity, drip
# ---------------------------------------------------------------------------


def test_charity_carve_out_is_exact_and_synchronous(db):
    """The public transparency figure is a ledger balance, so it must be right."""
    revenue = biglie("1000", VINTA)
    system_account(AccountKind.HOUSE_REVENUE, VINTA)

    recognise_revenue(amount=revenue, source="test")

    expected, _ = split_percentage(revenue, CHARITY_BASIS_POINTS)
    assert system_account(AccountKind.CHARITY_POOL, VINTA).balance == expected
    assert reconcile(record_anomalies=False) == []


def test_drip_pays_nothing_when_the_pool_is_dry(db, alice):
    """Example 2 from the brief, and the line that keeps it non-inflationary."""
    supply_before = circulating_supply(VINTA)

    result = drip(user=alice, amount=biglie("5", VINTA), idempotency_key="drip-1")

    assert result is None
    assert circulating_supply(VINTA) == supply_before
    assert not PendingCredit.objects.filter(user=alice).exists()


def test_drip_pays_out_of_a_funded_pool(db, alice):
    recognise_revenue(amount=biglie("1000", VINTA), source="test")
    pool_before = system_account(AccountKind.REDISTRIBUTION_POOL, VINTA).balance

    credit = drip(user=alice, amount=biglie("3", VINTA), idempotency_key="drip-2")

    assert credit is not None
    assert credit.amount == biglie("3", VINTA)
    after = system_account(AccountKind.REDISTRIBUTION_POOL, VINTA).balance
    assert after == pool_before - biglie("3", VINTA)
    assert reconcile(record_anomalies=False) == []

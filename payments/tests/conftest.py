"""Shared fixtures for the payments suite.

Every test here runs against `FakeProvider`: no network, no Stripe account, and
— importantly — a real HMAC signature on every webhook, so the tests exercise
the verification path rather than skipping past it.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from compliance.constants import KycStatus, KycTier
from compliance.models import KycVerification
from core.money import Amount, Asset
from payments.providers import get_provider, reset_fake_provider
from wallet.services import claim_safe, issue_for_deposit

PIENA = Asset.BIGLIA_PIENA
VINTA = Asset.BIGLIA_VINTA
DONO = Asset.BIGLIA_DONO


def euro(units: str) -> Amount:
    return Amount.from_major(units, Asset.EUR)


def biglie(units: str, asset: Asset = PIENA) -> Amount:
    return Amount.from_major(units, asset)


#: A structurally valid IBAN (mod-97 checks out). Never a real account.
TEST_IBAN = "IT60X0542811101000000123456"


@pytest.fixture(autouse=True)
def payment_settings(settings):
    """Play-money mode with a known webhook secret, isolated per test."""
    settings.LIVE_MODE = False
    settings.STRIPE_SECRET_KEY = ""
    settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
    reset_fake_provider()
    yield settings
    reset_fake_provider()


@pytest.fixture
def provider(payment_settings):
    """The same in-process fake the views and services will reach for."""
    return get_provider()


@pytest.fixture
def alice(db):
    return get_user_model().objects.create_user(
        email="alice@example.com", password="x" * 12, date_of_birth=date(1990, 5, 17)
    )


def approve_kyc(user, tier: KycTier = KycTier.BASIC) -> KycVerification:
    """Grant a verification tier the way `compliance` records one."""
    user.is_identity_verified = True
    user.save(update_fields=["is_identity_verified"])
    return KycVerification.objects.create(
        user=user, tier=tier, status=KycStatus.APPROVED, reviewed_at=timezone.now()
    )


@pytest.fixture
def verified(alice):
    approve_kyc(alice)
    return alice


def fund(user, units: str = "500"):
    """Give `user` spendable biglie piene the way a real deposit would.

    Issued, then claimed safely — so the 1% claim fee applies and the balances
    these tests assert on are the balances the product actually produces (500
    issued leaves 495 spendable).
    """
    credit = issue_for_deposit(
        user=user, euro=euro(units), biglie=biglie(units),
        idempotency_key=f"fixture-deposit-{user.pk}", reference_id="fixture-deposit",
    )
    claim_safe(credit=credit, idempotency_key=f"fixture-claim-{user.pk}")
    return user


@pytest.fixture
def funded(verified):
    """Verified Alice with 495 spendable biglie piene."""
    return fund(verified)


@pytest.fixture
def funded_unverified(alice):
    """Money in the account, no KYC on file. The withdrawal gate's test case."""
    return fund(alice)

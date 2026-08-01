"""Smoke tests: the pages actually render.

Unit tests prove the ledger is right. These prove the site is *up* — that the
templates resolve, the context processors run against a real request, and every
`{% url %}` in the chrome points at something that exists. A test suite that is
green while the home page 500s is worse than no test suite, because it is
reassuring.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from core.money import Amount, Asset
from wallet.constants import CreditSource
from wallet.services import credit_pending

PUBLIC_PAGES = ["web:home", "web:charity", "web:fairness", "identity:login", "identity:register"]

AUTHED_PAGES = [
    "web:dashboard",
    "compliance:limits",
    "compliance:self_exclusion",
    "compliance:self_exclusion_status",
    "compliance:kyc",
    "identity:profile",
]


@pytest.fixture
def player(db):
    return get_user_model().objects.create_user(
        email="player@example.com", password="x" * 12, date_of_birth=date(1990, 5, 4),
    )


@pytest.mark.parametrize("route", PUBLIC_PAGES)
def test_public_pages_render(client, db, route):
    response = client.get(reverse(route))
    assert response.status_code == 200


@pytest.mark.parametrize("route", AUTHED_PAGES)
def test_authenticated_pages_render(client, player, route):
    client.force_login(player)
    response = client.get(reverse(route))
    assert response.status_code == 200


def test_dashboard_shows_a_pending_credit_with_both_choices(client, player):
    """The claim funnel, rendered.

    Asserts both buttons are present — the safe path disappearing from the
    markup is the single most consequential regression this product could have.
    """
    credit_pending(
        user=player,
        amount=Amount.from_major("12.50", Asset.BIGLIA_VINTA),
        source=CreditSource.GAME_WIN,
        idempotency_key="smoke-1",
        reference_id="smoke-1",
    )

    client.force_login(player)
    body = client.get(reverse("web:dashboard")).content.decode()

    assert "Prendile e basta" in body
    assert "Rischiale" in body
    assert "◍12.50" in body


def test_claiming_safely_updates_the_page(client, player):
    credit = credit_pending(
        user=player,
        amount=Amount.from_major("10", Asset.BIGLIA_VINTA),
        source=CreditSource.GIFT,
        idempotency_key="smoke-2",
        reference_id="smoke-2",
    )

    client.force_login(player)
    response = client.post(
        reverse("web:claim_safe", kwargs={"credit_id": credit.pk}), follow=True
    )

    assert response.status_code == 200
    credit.refresh_from_db()
    assert credit.state == "claimed"


def test_a_user_cannot_claim_someone_elses_credit(client, player, db):
    """Ownership is scoped in the query, so this must be a 404, not a 200."""
    other = get_user_model().objects.create_user(email="other@example.com", password="x" * 12)
    credit = credit_pending(
        user=other,
        amount=Amount.from_major("99", Asset.BIGLIA_VINTA),
        source=CreditSource.GAME_WIN,
        idempotency_key="smoke-3",
        reference_id="smoke-3",
    )

    client.force_login(player)
    response = client.post(reverse("web:claim_safe", kwargs={"credit_id": credit.pk}))

    assert response.status_code == 404

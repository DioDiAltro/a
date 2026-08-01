"""Who is allowed through the door.

`can_wager` is the single predicate every game, lottery and duel consults before
taking a stake. It is a property on the user model rather than a check each
entry point performs, because the failure mode of the alternative is one
forgotten call site and a fifteen-year-old with a slot machine.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

User = get_user_model()


def born_years_ago(years: int, days: int = 0):
    return timezone.localdate() - timedelta(days=years * 365.25 + days)


# ---------------------------------------------------------------------------
# Age
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("years,expected", [(17, False), (18, True), (40, True)])
def test_age_decides_whether_an_account_may_wager(db, years, expected):
    user = User.objects.create_user(
        email=f"u{years}@example.com", password="x" * 12,
        date_of_birth=born_years_ago(years, days=2),
    )
    assert user.is_of_age is expected
    assert user.can_wager is expected


def test_a_missing_date_of_birth_is_not_a_pass(db):
    """An unverified date of birth is a legal blocker, not a profile gap.

    Defaulting to "allowed" here would mean any account created outside the
    registration form — a fixture, a data migration, a support tool — could
    gamble.
    """
    user = User.objects.create_user(email="unknown@example.com", password="x" * 12)
    assert user.date_of_birth is None
    assert not user.is_of_age
    assert not user.can_wager


def test_the_boundary_is_the_actual_birthday(db):
    """Someone turning 18 tomorrow is 17 today. Off-by-one here is the
    difference between compliant and not."""
    with freeze_time("2026-06-15"):
        user = User.objects.create_user(
            email="tomorrow@example.com", password="x" * 12,
            date_of_birth=timezone.localdate().replace(year=2008, month=6, day=16),
        )
        assert not user.is_of_age

    with freeze_time("2026-06-16"):
        user.refresh_from_db()
        assert user.is_of_age


def test_registration_refuses_a_minor(db, client):
    """The form must refuse too — `can_wager` blocking play afterwards would
    still leave an under-age account holding money."""
    response = client.post(
        reverse("identity:register"),
        {
            "email": "ragazzino@example.com",
            "password1": "un-lungo-segreto-99",
            "password2": "un-lungo-segreto-99",
            "date_of_birth": born_years_ago(15).isoformat(),
            "accepts_terms": "on",
        },
    )

    assert response.status_code == 200, "a refused registration should redisplay the form"
    assert not User.objects.filter(email="ragazzino@example.com").exists()


def test_registration_accepts_an_adult(db, client):
    response = client.post(
        reverse("identity:register"),
        {
            "email": "adulta@example.com",
            "password1": "un-lungo-segreto-99",
            "password2": "un-lungo-segreto-99",
            "date_of_birth": born_years_ago(30).isoformat(),
            "accepts_terms": "on",
        },
    )

    user = User.objects.filter(email="adulta@example.com").first()
    assert user is not None, f"registration failed: {response.context['form'].errors if response.context and 'form' in response.context else response.status_code}"
    assert user.can_wager


# ---------------------------------------------------------------------------
# Account state
# ---------------------------------------------------------------------------


def test_a_self_excluded_adult_cannot_wager(db):
    user = User.objects.create_user(
        email="pausa@example.com", password="x" * 12, date_of_birth=born_years_ago(30),
    )
    assert user.can_wager

    user.is_self_excluded = True
    assert not user.can_wager


def test_a_deactivated_account_cannot_wager(db):
    user = User.objects.create_user(
        email="chiuso@example.com", password="x" * 12, date_of_birth=born_years_ago(30),
    )
    user.is_active = False
    assert not user.can_wager


def test_email_is_the_identifier_and_is_unique(db):
    User.objects.create_user(
        email="uno@example.com", password="x" * 12, date_of_birth=born_years_ago(30)
    )
    with pytest.raises(IntegrityError):
        User.objects.create_user(
            email="uno@example.com", password="x" * 12, date_of_birth=born_years_ago(30)
        )


def test_an_account_needs_an_email(db):
    with pytest.raises(ValueError):
        User.objects.create_user(email="", password="x" * 12)


def test_handle_falls_back_to_the_local_part(db):
    """Users appear next to each other in duels and lottery lists; showing a
    full email address there would leak it to everyone in the draw."""
    user = User.objects.create_user(
        email="marta.rossi@example.com", password="x" * 12, date_of_birth=born_years_ago(30)
    )
    assert user.handle == "marta.rossi"
    assert "@" not in user.handle

    user.display_name = "Marta"
    assert user.handle == "Marta"

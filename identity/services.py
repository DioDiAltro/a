"""The identity context's public API.

Other contexts import from this module and nothing else in `identity` (plus
`identity.models` where Django itself forces it, i.e. `AUTH_USER_MODEL`).
"""

from __future__ import annotations

from datetime import date

from django.db import transaction
from django.utils import timezone

from core.contracts import UserRegistered
from core.events import bus
from core.exceptions import AgeVerificationRequired
from core.money import Amount, Asset
from identity.levels import (
    LEVELS,
    Level,
    award_xp,
    can_stake_duel,
    level_for_xp,
    level_of,
    max_duel_stake,
)
from identity.models import PlayerLevel, SellerProfile, User

__all__ = [
    "LEVELS",
    "MINIMUM_AGE",
    "Level",
    "age_in_years",
    "award_xp",
    "can_stake_duel",
    "ensure_seller_profile",
    "is_of_age",
    "level_for_xp",
    "level_of",
    "max_duel_stake",
    "register_user",
    "touch_last_seen",
]

#: The platform settles games of chance for money. This is a legal floor, not a
#: product setting, which is why it lives in code and not in the database.
MINIMUM_AGE = 18


def age_in_years(date_of_birth: date, today: date | None = None) -> int:
    """Completed years, counted the way a birthday is counted."""
    today = today or timezone.localdate()
    years = today.year - date_of_birth.year
    if (today.month, today.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return years


def is_of_age(date_of_birth: date | None, today: date | None = None) -> bool:
    if date_of_birth is None:
        return False
    return age_in_years(date_of_birth, today) >= MINIMUM_AGE


@transaction.atomic
def register_user(
    *,
    email: str,
    password: str,
    date_of_birth: date,
    display_name: str = "",
    **extra,
) -> User:
    """Create an account.

    The age check is repeated here even though the form already ran it: a
    registration that arrives through an API, a management command or a future
    social login must hit the same floor, and "the form validated it" is not a
    guarantee that survives refactoring.
    """
    if not is_of_age(date_of_birth):
        raise AgeVerificationRequired(
            "Devi avere almeno 18 anni per aprire un conto su biglie."
        )

    user = User.objects.create_user(
        email=email,
        password=password,
        date_of_birth=date_of_birth,
        display_name=display_name,
        **extra,
    )
    PlayerLevel.objects.create(user=user)

    bus.publish(UserRegistered(user_id=user.id))
    return user


def ensure_seller_profile(user, *, shop_name: str = "") -> SellerProfile:
    """Get or create the user's selling identity.

    Called by `marketplace` on first listing, and by the profile view when the
    user opens a shop deliberately.
    """
    profile, created = SellerProfile.objects.get_or_create(
        user=user, defaults={"shop_name": shop_name or user.handle}
    )
    if created and shop_name:
        profile.shop_name = shop_name
        profile.save(update_fields=["shop_name"])
    return profile


def touch_last_seen(user) -> None:
    """Record activity. Feeds the idle-user drip in `economy`."""
    User.objects.filter(pk=user.pk).update(last_seen_at=timezone.now())


def wagering_denial_reason(user) -> str | None:
    """Why this user may not wager, in Italian, or None if they may.

    A single place for the phrasing so every context refuses in the same words.
    """
    if not user.is_active:
        return "Il tuo account non è attivo."
    if not user.is_of_age:
        return "Devi avere almeno 18 anni per giocare."
    if user.is_self_excluded:
        return "Hai attivato l'autoesclusione."
    return None


def duel_ceiling(user, asset: Asset = Asset.BIGLIA_PIENA) -> Amount:
    """Alias kept explicit for `games`, whose vocabulary is 'ceiling'."""
    return max_duel_stake(user, asset)

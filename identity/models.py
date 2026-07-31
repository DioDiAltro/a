"""Users.

`User` lives here because `AUTH_USER_MODEL` is a project-wide contract that must
be settled before any other context can declare a foreign key. Everything richer
— KYC evidence, limits, self-exclusion — belongs to `compliance`, which links
back by id.
"""

from __future__ import annotations

import uuid
from datetime import date

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class UserManager(BaseUserManager):
    """Email-based manager. There are no usernames on this platform."""

    use_in_migrations = True

    def _create(self, email: str, password: str | None, **extra):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("date_of_birth", date(1970, 1, 1))
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("A superuser must have is_staff and is_superuser set.")
        return self._create(email, password, **extra)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    username = None  # replaced by email
    email = models.EmailField(_("email address"), unique=True)

    display_name = models.CharField(max_length=40, blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True)

    #: Required at registration. The platform runs games of chance for money, so
    #: an unverified date of birth is not a profile gap — it is a legal blocker,
    #: and `is_of_age` gates every wagering entry point.
    date_of_birth = models.DateField(null=True, blank=True)

    #: Set by `compliance` once documents check out. Denormalised here because
    #: it is read on nearly every request and a join per page is not worth it.
    is_identity_verified = models.BooleanField(default=False)

    #: Mirrors the active self-exclusion in `compliance`. The middleware reads
    #: this rather than querying the exclusion table on every request.
    is_self_excluded = models.BooleanField(default=False, db_index=True)

    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        indexes = [models.Index(fields=["is_self_excluded", "last_seen_at"])]

    def __str__(self) -> str:
        return self.display_name or self.email

    @property
    def is_of_age(self) -> bool:
        if self.date_of_birth is None:
            return False
        today = timezone.localdate()
        years = today.year - self.date_of_birth.year
        if (today.month, today.day) < (self.date_of_birth.month, self.date_of_birth.day):
            years -= 1
        return years >= 18

    @property
    def can_wager(self) -> bool:
        """The single predicate every game and lottery entry point must consult."""
        return self.is_active and self.is_of_age and not self.is_self_excluded

    @property
    def handle(self) -> str:
        return self.display_name or self.email.split("@")[0]


class SellerProfile(models.Model):
    """A user's selling identity. Created lazily on first listing.

    Split from `User` because most accounts never sell, and because the duel
    mechanic (sellers challenging sellers for each other's biglie) needs a
    reputation surface that only exists for people who actually trade.
    """

    user = models.OneToOneField(
        User, primary_key=True, on_delete=models.CASCADE, related_name="seller_profile"
    )
    shop_name = models.CharField(max_length=60)
    bio = models.TextField(blank=True)

    sales_count = models.PositiveIntegerField(default=0)
    rating_total = models.PositiveIntegerField(default=0)
    rating_votes = models.PositiveIntegerField(default=0)

    #: Duels are opt-in. A seller who wants to be left alone must be able to
    #: say so, otherwise the mechanic becomes harassment rather than sport.
    accepts_duels = models.BooleanField(default=True)
    duels_won = models.PositiveIntegerField(default=0)
    duels_lost = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.shop_name

    @property
    def rating(self) -> float | None:
        if not self.rating_votes:
            return None
        return round(self.rating_total / self.rating_votes, 2)

"""Shared model primitives."""

from __future__ import annotations

import uuid

from django.db import models

from core.money import Amount, Asset


class UUIDModel(models.Model):
    """UUID primary keys everywhere.

    Sequential integer ids leak volume ("we are on order #412") and make it
    trivial to enumerate other people's listings and games. On a platform where
    ids appear in shareable game-verification URLs that matters.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class BaseModel(UUIDModel, TimestampedModel):
    class Meta:
        abstract = True


class AmountField(models.BigIntegerField):
    """Stores minor units. BigInteger because a jackpot in scaglie gets large.

    Deliberately *not* a Decimal field: the whole point of `core.money` is that
    money never becomes a fractional type on its way through the system.
    """

    description = "A quantity of an asset, in minor units"


class AmountMixin:
    """Pairs an `AmountField` with an asset column into an `Amount`.

    Concrete models declare ``amount_minor`` and ``asset``; this exposes them as
    a single value object so domain code never handles the two halves apart.
    """

    amount_minor: int
    asset: str

    @property
    def amount(self) -> Amount:
        return Amount(self.amount_minor, Asset(self.asset))

    @amount.setter
    def amount(self, value: Amount) -> None:
        self.amount_minor = value.minor
        self.asset = value.asset


class IdempotencyKey(BaseModel):
    """A record that a named operation already ran.

    Every money-moving service takes an idempotency key and writes one of these
    inside the same transaction, with a unique constraint. A retried request —
    a double-clicked "Riscatta", a Stripe webhook redelivery, a Celery task that
    ran twice — hits the constraint and the caller returns the original result
    instead of paying twice.
    """

    key = models.CharField(max_length=200, unique=True)
    scope = models.CharField(max_length=60, db_index=True)
    #: Hash of the request payload, so a replayed key carrying *different* data
    #: is caught as a conflict rather than silently returning the wrong result.
    payload_fingerprint = models.CharField(max_length=64)
    result_ref = models.CharField(max_length=200, blank=True)

    class Meta:
        indexes = [models.Index(fields=["scope", "created_at"])]

    def __str__(self) -> str:
        return f"{self.scope}:{self.key}"

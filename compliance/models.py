"""Compliance state: who we verified, what they limited, who walked away.

Every model here is about a control that must *work*, not about a page that must
look responsible. The two that carry the most weight:

* `PlayerLimit`, where the asymmetry between lowering and raising a limit lives.
  A limit you can raise the instant you hit it is not a limit.
* `AuditEvent`, an append-only journal chained by hash. Rows refuse to be
  updated through the ORM, and the chain makes a row rewritten *behind* the ORM
  detectable rather than merely unlikely.

This context links to users by `settings.AUTH_USER_MODEL` and never imports
`identity` internals.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from compliance.constants import (
    GENESIS_HASH,
    PERIOD_WINDOW,
    DocumentType,
    ExclusionState,
    KycStatus,
    KycTier,
    LimitKind,
    LimitPeriod,
    tier_rank,
)
from core.exceptions import IllegalStateTransition
from core.models import BaseModel
from core.money import Amount, Asset


class KycVerification(BaseModel):
    """One submitted verification attempt.

    Modelled as an append-only-ish history rather than a single mutable row per
    user: an upgrade from BASIC to FULL is a new submission with its own
    documents and its own reviewer, and "when did we know what" is a question
    an auditor will ask.

    Note what is *not* stored: no document images, no numbers, no scans. Only
    the reference the identity provider gave us. A leak of this table must not
    be a leak of anyone's passport.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="kyc_verifications"
    )
    tier = models.CharField(max_length=10, choices=KycTier.choices, default=KycTier.BASIC)
    status = models.CharField(
        max_length=10, choices=KycStatus.choices, default=KycStatus.PENDING, db_index=True
    )

    document_type = models.CharField(max_length=20, choices=DocumentType.choices, blank=True)
    #: Opaque handle at the verification provider. Never the document itself.
    document_reference = models.CharField(max_length=120, blank=True)
    document_country = models.CharField(max_length=2, blank=True)

    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="kyc_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    #: Documents go stale. A verification past this date stops counting.
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "status", "tier"])]

    def __str__(self) -> str:
        return f"{self.user_id} · {self.get_tier_display()} · {self.get_status_display()}"

    def is_effective(self, now: datetime | None = None) -> bool:
        now = now or timezone.now()
        if self.status != KycStatus.APPROVED:
            return False
        return self.expires_at is None or self.expires_at > now

    @property
    def rank(self) -> int:
        return tier_rank(self.tier)


class PlayerLimit(BaseModel):
    """A limit the player set on themselves.

    `value` is minor units for the money kinds and minutes for SESSION_TIME.

    The cooling-off pair is the whole point. `value` is what is enforced right
    now; `pending_value` with `effective_from` is a *loosening* that has been
    asked for and has not matured yet. Tightening never uses the pair — it is
    written straight into `value`.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="limits"
    )
    kind = models.CharField(max_length=16, choices=LimitKind.choices)
    period = models.CharField(max_length=8, choices=LimitPeriod.choices)

    value = models.BigIntegerField()
    pending_value = models.BigIntegerField(null=True, blank=True)
    effective_from = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind", "period"], name="unique_limit_per_user_kind_period"
            ),
            models.CheckConstraint(check=models.Q(value__gte=0), name="limit_value_non_negative"),
        ]
        ordering = ("kind", "period")

    def __str__(self) -> str:
        return f"{self.user_id} · {self.get_kind_display()} · {self.get_period_display()}"

    # -- the asymmetry ------------------------------------------------------

    def effective_value(self, now: datetime | None = None) -> int:
        """What is enforced at `now`.

        A pending raise counts only once its cooling-off has elapsed. This is a
        pure read: `services.apply_matured_limits` is what writes the matured
        value back, so a user who never returns still has the lower limit
        enforced by every call site.
        """
        now = now or timezone.now()
        if (
            self.pending_value is not None
            and self.effective_from is not None
            and self.effective_from <= now
        ):
            return self.pending_value
        return self.value

    @property
    def has_pending_change(self) -> bool:
        return self.pending_value is not None and self.effective_from is not None

    def window(self) -> timedelta:
        return PERIOD_WINDOW[self.period]

    def as_amount(self, asset: Asset = Asset.BIGLIA_PIENA) -> Amount | None:
        """The limit as money, or None for SESSION_TIME which is minutes."""
        if self.kind == LimitKind.SESSION_TIME:
            return None
        return Amount(self.effective_value(), asset)


class LimitUsage(BaseModel):
    """What the player has actually spent, lost or wagered.

    `compliance` cannot ask `wallet` (it is not allowed to depend on it, and
    should not be), so usage is accumulated here from the money events on the
    bus. That makes the limit checker independent of the ledger's internals and
    keeps this context liftable into its own service.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="limit_usage"
    )
    kind = models.CharField(max_length=16, choices=LimitKind.choices)
    #: Minor units for money kinds, minutes for SESSION_TIME.
    amount_minor = models.BigIntegerField()
    asset = models.CharField(max_length=16, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    reference = models.CharField(max_length=120, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user", "kind", "occurred_at"])]
        ordering = ("-occurred_at",)

    def __str__(self) -> str:
        return f"{self.user_id} · {self.kind} · {self.amount_minor}"


class SelfExclusion(BaseModel):
    """The cord the player pulled.

    Temporary exclusions carry an `until`; permanent ones do not. Neither can be
    revoked early — see `services.lift_self_exclusion`. The model refuses on its
    own behalf too, so a future admin action or data migration cannot quietly
    undo it by setting a field.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="self_exclusions"
    )
    started_at = models.DateTimeField(default=timezone.now)
    #: None means permanent.
    until = models.DateTimeField(null=True, blank=True)
    state = models.CharField(
        max_length=10, choices=ExclusionState.choices, default=ExclusionState.ACTIVE,
        db_index=True,
    )
    reason = models.TextField(blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-started_at",)
        indexes = [models.Index(fields=["user", "state"])]

    def __str__(self) -> str:
        return f"{self.user_id} · {'permanente' if self.is_permanent else self.until:%Y-%m-%d}"

    @property
    def is_permanent(self) -> bool:
        return self.until is None

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or timezone.now()
        if self.state != ExclusionState.ACTIVE:
            return False
        return self.is_permanent or self.until > now

    def is_due_to_end(self, now: datetime | None = None) -> bool:
        now = now or timezone.now()
        return (
            self.state == ExclusionState.ACTIVE
            and not self.is_permanent
            and self.until <= now
        )

    def remaining(self, now: datetime | None = None) -> timedelta | None:
        if self.is_permanent:
            return None
        now = now or timezone.now()
        return max(timedelta(0), self.until - now)


class RealityCheckSetting(BaseModel):
    """How often to interrupt the player with the truth about elapsed time.

    Kept per user and switchable, but not switchable *off* below the floor: the
    interval is clamped in `services.set_reality_check`.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reality_check"
    )
    enabled = models.BooleanField(default=True)
    interval_minutes = models.PositiveSmallIntegerField(default=60)
    last_notified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.user_id} · ogni {self.interval_minutes} min"


class AuditEvent(models.Model):
    """Append-only AML / audit journal, chained by hash.

    Two independent guards:

    1. `save()` refuses to write a row that already exists, and `delete()`
       refuses outright. That stops ordinary code — an admin action, a careless
       `obj.field = x; obj.save()` — from rewriting history.
    2. Each row stores ``sha256(previous_hash + canonical_payload)``. That is
       the guard that matters, because it also catches what (1) cannot: a raw
       ``UPDATE``, a compromised database user, a restored-and-edited backup.
       Recomputing the chain shows exactly which row stopped matching.

    An integer primary key, not a UUID: the chain needs a total order, and
    "the row before this one" must be a cheap, unambiguous query.
    """

    id = models.BigAutoField(primary_key=True)

    #: `DomainEvent.name`, e.g. "payments.deposit_confirmed".
    event_name = models.CharField(max_length=60, db_index=True)
    #: The id of the originating domain event, for tracing back to the source.
    event_id = models.UUIDField(null=True, blank=True)

    #: Not a ForeignKey on purpose: the journal must outlive the account it
    #: describes. Deleting a user must not cascade away the record that they
    #: withdrew forty thousand euros.
    user_id = models.UUIDField(null=True, blank=True, db_index=True)

    amount_minor = models.BigIntegerField(null=True, blank=True)
    asset = models.CharField(max_length=16, blank=True)

    payload = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(db_index=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    previous_hash = models.CharField(max_length=64)
    hash_chain = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ("id",)
        indexes = [models.Index(fields=["user_id", "event_name", "occurred_at"])]

    def __str__(self) -> str:
        return f"#{self.id} {self.event_name} {self.hash_chain[:12]}"

    # -- the chain ----------------------------------------------------------

    def canonical_payload(self) -> str:
        """The exact bytes the hash is taken over.

        Sorted keys, no whitespace, `default=str` for UUIDs and datetimes — the
        same recipe as `core.idempotency.fingerprint`, so two people computing
        it by hand get the same answer.
        """
        body = {
            "event_name": self.event_name,
            "event_id": str(self.event_id) if self.event_id else None,
            "user_id": str(self.user_id) if self.user_id else None,
            "amount_minor": self.amount_minor,
            "asset": self.asset,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "payload": self.payload,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def compute_hash(previous_hash: str, canonical: str) -> str:
        return hashlib.sha256(f"{previous_hash}{canonical}".encode()).hexdigest()

    def expected_hash(self) -> str:
        return self.compute_hash(self.previous_hash, self.canonical_payload())

    def is_intact(self) -> bool:
        return self.hash_chain == self.expected_hash()

    # -- append-only --------------------------------------------------------

    def save(self, *args, **kwargs):  # noqa: DJ012 - hash helpers must be read first
        if not self._state.adding:
            raise IllegalStateTransition(
                current="recorded",
                attempted="update",
                message=(
                    f"AuditEvent #{self.pk} is part of an append-only journal. "
                    "Record a correcting entry instead of editing history."
                ),
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise IllegalStateTransition(
            current="recorded",
            attempted="delete",
            message="Audit journal rows are never deleted.",
        )

    # -- appending ----------------------------------------------------------

    @classmethod
    def record(cls, *, event_name: str, user_id=None, amount_minor: int | None = None,
               asset: str = "", payload: dict | None = None, event_id=None,
               occurred_at=None) -> AuditEvent:
        """Append one row, linked to the tip of the chain.

        Takes a row lock on the current tip so two concurrent writers cannot
        both chain off the same predecessor and produce a fork. Under contention
        this serialises the journal, which is the correct trade: an audit log
        that is fast and occasionally forked is not an audit log.
        """
        tip = cls.objects.select_for_update().order_by("-id").first()
        previous = tip.hash_chain if tip is not None else GENESIS_HASH

        event = cls(
            event_name=event_name,
            event_id=event_id,
            user_id=user_id,
            amount_minor=amount_minor,
            asset=asset,
            payload=payload or {},
            occurred_at=occurred_at or timezone.now(),
            previous_hash=previous,
        )
        event.hash_chain = cls.compute_hash(previous, event.canonical_payload())
        event.save()
        return event

    @classmethod
    def verify_chain(cls, *, limit: int | None = None) -> list[int]:
        """Walk the journal and return the ids of any rows that no longer verify.

        An empty list is the only acceptable result. Anything else means the
        table was written to by something other than `record`, and the question
        stops being "is there a bug" and starts being "who has database access".
        """
        broken: list[int] = []
        previous = GENESIS_HASH

        queryset = cls.objects.order_by("id")
        if limit is not None:
            queryset = queryset[:limit]

        for event in queryset.iterator():
            if event.previous_hash != previous or not event.is_intact():
                broken.append(event.id)
            previous = event.hash_chain

        return broken


GENESIS = GENESIS_HASH

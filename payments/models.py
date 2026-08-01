"""Deposits and withdrawals — the only two places real euros touch the platform.

These rows are the bridge between the acquirer's world and the ledger's. They
are deliberately *not* the source of truth for money: the ledger is. A `Deposit`
in state SUCCEEDED asserts "we told the wallet to issue"; the wallet's journal
entry is what proves it happened. Reconciliation compares the two, and a
mismatch is an incident.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.exceptions import IllegalStateTransition
from core.models import AmountField, BaseModel
from core.money import Amount, Asset
from payments.constants import (
    DEPOSIT_TRANSITIONS,
    WITHDRAWAL_TRANSITIONS,
    PaymentState,
    ProviderKey,
)

STATE_CHOICES = [(s.value, s.label) for s in PaymentState]
PROVIDER_CHOICES = [(p.value, p.value) for p in ProviderKey]


class StateMachineMixin:
    """A guarded `state` column.

    Every illegal move raises rather than being silently ignored, because the
    illegal moves are the interesting ones: "SUCCEEDED → SUCCEEDED" is a double
    credit and "FAILED → SUCCEEDED" is a resurrected chargeback.
    """

    #: Set on each concrete model.
    TRANSITIONS: dict[PaymentState, frozenset[PaymentState]] = {}

    state: str

    def can_transition_to(self, target: PaymentState) -> bool:
        return target in self.TRANSITIONS.get(PaymentState(self.state), frozenset())

    def transition_to(self, target: PaymentState, **extra_fields) -> None:
        """Move to `target`, or raise. Saves the row with any timestamp fields."""
        if not self.can_transition_to(target):
            raise IllegalStateTransition(current=str(self.state), attempted=str(target))

        self.state = target
        for name, value in extra_fields.items():
            setattr(self, name, value)
        self.save(update_fields=["state", "updated_at", *extra_fields])

    @property
    def state_label(self) -> str:
        return PaymentState(self.state).label


class DepositQuerySet(models.QuerySet):
    def settled(self):
        return self.filter(state=PaymentState.SUCCEEDED)

    def for_user(self, user):
        return self.filter(user=user)

    def awaiting_provider(self):
        return self.filter(state=PaymentState.PENDING)


class Deposit(StateMachineMixin, BaseModel):
    """Euros in, biglie out. The only inflationary event in the platform."""

    TRANSITIONS = DEPOSIT_TRANSITIONS

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="deposits"
    )

    #: What the user is charged, and what we will issue for it. Both are stored
    #: rather than derived so a later change to EURO_PER_BIGLIA cannot rewrite
    #: history.
    euro_minor = AmountField()
    biglie_minor = AmountField()

    state = models.CharField(
        max_length=16, choices=STATE_CHOICES, default=PaymentState.INITIATED, db_index=True
    )

    provider = models.CharField(max_length=16, choices=PROVIDER_CHOICES)
    #: The PaymentIntent id. Unique so a provider object can never be attached
    #: to two of our deposits.
    provider_reference = models.CharField(max_length=120, blank=True, db_index=True)
    #: Short-lived and designed to be handed to the browser; still never logged.
    client_secret = models.CharField(max_length=255, blank=True)

    failure_code = models.CharField(max_length=60, blank=True)
    failure_message = models.CharField(max_length=255, blank=True)

    confirmed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)

    metadata = models.JSONField(default=dict, blank=True)

    objects = DepositQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["user", "state", "created_at"]),
            models.Index(fields=["state", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(euro_minor__gt=0), name="deposit_euro_positive"),
            models.CheckConstraint(check=Q(biglie_minor__gt=0), name="deposit_biglie_positive"),
            models.UniqueConstraint(
                fields=["provider", "provider_reference"],
                condition=~Q(provider_reference=""),
                name="uniq_provider_reference",
            ),
        ]

    def __str__(self) -> str:
        return f"deposit {self.euro} by {self.user_id} ({self.state})"

    @property
    def euro(self) -> Amount:
        return Amount(self.euro_minor, Asset.EUR)

    @property
    def biglie(self) -> Amount:
        return Amount(self.biglie_minor, Asset.BIGLIA_PIENA)

    def mark_confirmed(self) -> None:
        self.transition_to(PaymentState.SUCCEEDED, confirmed_at=timezone.now())

    def mark_failed(self, *, code: str = "", message: str = "") -> None:
        self.transition_to(
            PaymentState.FAILED,
            failed_at=timezone.now(),
            failure_code=code[:60],
            failure_message=message[:255],
        )

    def mark_reversed(self, *, reason: str = "") -> None:
        self.transition_to(
            PaymentState.REVERSED,
            reversed_at=timezone.now(),
            failure_message=reason[:255],
        )


class WithdrawalQuerySet(models.QuerySet):
    def awaiting_review(self):
        return self.filter(state=PaymentState.PENDING, requires_review=True)

    def settled(self):
        return self.filter(state=PaymentState.SUCCEEDED)


class Withdrawal(StateMachineMixin, BaseModel):
    """Biglie piene out, euros back to a bank account.

    Only BIGLIA_PIENA can be here at all — `payments.services.open_withdrawal`
    refuses anything else before a row is ever created. The asset column exists
    so the refusal is visible in the data model rather than only in a docstring.
    """

    TRANSITIONS = WITHDRAWAL_TRANSITIONS

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="withdrawals"
    )

    biglie_minor = AmountField()
    asset = models.CharField(
        max_length=16, choices=[(Asset.BIGLIA_PIENA.value, Asset.BIGLIA_PIENA.label)],
        default=Asset.BIGLIA_PIENA,
    )
    euro_minor = AmountField()

    state = models.CharField(
        max_length=16, choices=STATE_CHOICES, default=PaymentState.INITIATED, db_index=True
    )

    #: A token from the payout provider, never a raw IBAN. The last four digits
    #: are kept only so the user can tell two of their accounts apart.
    destination_reference = models.CharField(max_length=120, blank=True)
    destination_last4 = models.CharField(max_length=4, blank=True)

    #: The wallet hold that reserves the funds while this is in flight. Stored by
    #: id, not by foreign key: `payments` must not depend on wallet's tables.
    hold_id = models.UUIDField(null=True, blank=True)

    requires_review = models.BooleanField(default=False, db_index=True)
    approved_automatically = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="withdrawal_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)

    provider = models.CharField(max_length=16, choices=PROVIDER_CHOICES, blank=True)
    provider_reference = models.CharField(max_length=120, blank=True, db_index=True)

    confirmed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)

    objects = WithdrawalQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["user", "state", "created_at"]),
            models.Index(fields=["state", "requires_review"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(biglie_minor__gt=0), name="withdrawal_positive"),
            models.CheckConstraint(
                check=Q(asset=Asset.BIGLIA_PIENA.value), name="withdrawal_only_piena"
            ),
        ]

    def __str__(self) -> str:
        return f"withdrawal {self.biglie} by {self.user_id} ({self.state})"

    @property
    def biglie(self) -> Amount:
        return Amount(self.biglie_minor, Asset(self.asset))

    @property
    def euro(self) -> Amount:
        return Amount(self.euro_minor, Asset.EUR)

    @property
    def is_open(self) -> bool:
        return PaymentState(self.state) in (PaymentState.INITIATED, PaymentState.PENDING)


class ProviderEventLog(BaseModel):
    """Every webhook we accepted, kept verbatim.

    The idempotency table already stops a redelivery from crediting twice; this
    is the audit copy — when someone asks "why does this wallet have 40 biglie",
    the answer has to be a stored, signed payload rather than a log line that
    rotated away.
    """

    provider = models.CharField(max_length=16, choices=PROVIDER_CHOICES)
    event_id = models.CharField(max_length=120)
    event_type = models.CharField(max_length=80, db_index=True)
    payload = models.JSONField(default=dict)
    handled = models.BooleanField(default=False)
    #: What we did about it, in one line, for the audit reader.
    outcome = models.CharField(max_length=255, blank=True)
    deposit = models.ForeignKey(
        Deposit, null=True, blank=True, on_delete=models.SET_NULL, related_name="events"
    )
    withdrawal = models.ForeignKey(
        Withdrawal, null=True, blank=True, on_delete=models.SET_NULL, related_name="events"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_id"], name="uniq_provider_event"
            ),
        ]
        indexes = [models.Index(fields=["event_type", "created_at"])]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event_id} ({self.event_type})"

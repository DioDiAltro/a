"""The ledger.

Everything else in this platform is a game. This is the accounting, and it is
written to a stricter standard than the rest of the codebase:

* Value only ever moves through a `JournalEntry` with `Posting` rows that sum to
  zero **per asset**. There is no code path that increments a balance directly.
* `Account.balance_minor` is a cache. `Posting` is the truth.
  `reconcile()` proves the two agree, and CI fails if they ever don't.
* Nothing here trusts its caller. Services validate; the database enforces.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone

from core.models import AmountField, BaseModel
from core.money import Amount, Asset
from wallet.constants import (
    SYSTEM_KINDS,
    AccountKind,
    CreditSource,
    CreditState,
    EntryKind,
    HoldState,
)

ASSET_CHOICES = [(a.value, a.label) for a in Asset]


class AccountQuerySet(models.QuerySet):
    def for_user(self, user, asset: Asset | None = None):
        qs = self.filter(owner=user)
        return qs.filter(asset=asset) if asset else qs

    def system(self, kind: AccountKind, asset: Asset) -> Account:
        account, _ = self.get_or_create(
            owner=None, kind=kind, asset=asset, defaults={"balance_minor": 0}
        )
        return account


class Account(BaseModel):
    """A bucket that value can sit in.

    User accounts are (owner, kind, asset). System accounts have no owner and
    are singletons per (kind, asset).
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,  # never orphan a posting
        related_name="wallet_accounts",
    )
    kind = models.CharField(max_length=32, choices=[(k, k) for k in AccountKind])
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)

    #: Materialised sum of this account's postings. Maintained by the ledger
    #: service under a row lock; never written by anything else.
    balance_minor = AmountField(default=0)

    objects = AccountQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "kind", "asset"],
                condition=Q(owner__isnull=False),
                name="uniq_user_account",
            ),
            models.UniqueConstraint(
                fields=["kind", "asset"],
                condition=Q(owner__isnull=True),
                name="uniq_system_account",
            ),
            # A user can never be overdrawn. System accounts can and must go
            # negative — ISSUANCE going negative *is* the record of biglie in
            # circulation — so the constraint is scoped to owned accounts.
            models.CheckConstraint(
                check=Q(owner__isnull=True) | Q(balance_minor__gte=0),
                name="user_accounts_never_negative",
            ),
        ]
        indexes = [models.Index(fields=["owner", "asset", "kind"])]

    def __str__(self) -> str:
        who = self.owner_id or "system"
        return f"{who}/{self.kind}/{self.asset}"

    @property
    def balance(self) -> Amount:
        return Amount(self.balance_minor, Asset(self.asset))

    def recompute(self) -> Amount:
        """Sum the postings. The authoritative answer, at the cost of a scan."""
        total = self.postings.aggregate(total=Sum("amount_minor"))["total"] or 0
        return Amount(total, Asset(self.asset))


class JournalEntry(BaseModel):
    """One balanced movement of value. The unit of auditability.

    An entry is immutable once written. Corrections are new entries of kind
    ADJUSTMENT that reference the original — never an UPDATE, because a ledger
    you can edit is not a ledger.
    """

    kind = models.CharField(max_length=32, choices=[(k, k) for k in EntryKind], db_index=True)
    description = models.CharField(max_length=255, blank=True)

    #: What in the domain caused this. Kept as loose strings rather than a
    #: GenericForeignKey so the ledger has no import dependency on any other
    #: context — it must remain the one app nothing else can break.
    reference_type = models.CharField(max_length=60, blank=True, db_index=True)
    reference_id = models.CharField(max_length=64, blank=True, db_index=True)

    idempotency_key = models.CharField(max_length=200, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name_plural = "journal entries"
        indexes = [
            models.Index(fields=["reference_type", "reference_id"]),
            models.Index(fields=["kind", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.id}"

    def imbalance(self) -> dict[str, int]:
        """Per-asset sums. Every value must be 0 for the entry to be valid."""
        rows = self.postings.values("asset").annotate(total=Sum("amount_minor"))
        return {r["asset"]: r["total"] for r in rows if r["total"] != 0}


class Posting(BaseModel):
    """A signed movement against one account. Immutable."""

    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="postings")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="postings")
    amount_minor = AmountField()  # signed: negative debits, positive credits
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "created_at"]),
            models.Index(fields=["entry"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=~Q(amount_minor=0), name="posting_never_zero"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.account} {self.amount_minor:+d} {self.asset}"

    @property
    def amount(self) -> Amount:
        return Amount(self.amount_minor, Asset(self.asset))


class PendingCreditQuerySet(models.QuerySet):
    def claimable(self, user):
        return self.filter(
            user=user, state=CreditState.PENDING, expires_at__gt=timezone.now()
        ).order_by("expires_at")

    def stale(self):
        return self.filter(state=CreditState.PENDING, expires_at__lte=timezone.now())


class PendingCredit(BaseModel):
    """Value that has arrived for a user but is not yet theirs to spend.

    This is the heart of the product. Every inbound biglia — prize, gift, sale
    proceeds, drip, refund — lands here first, and the user must open the
    dashboard and decide:

      * **claim safe**  → they receive it, less a 1% fee
      * **claim risky** → a minigame decides; they may double it or lose it

    Because there is no bypass, every single biglia entering the economy is an
    opportunity for the user to feel something. It also gives the platform one
    place to enforce limits, one funnel to measure, and one expiry policy.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="pending_credits"
    )
    amount_minor = AmountField()
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)
    source = models.CharField(max_length=32, choices=[(s, s) for s in CreditSource])
    state = models.CharField(
        max_length=16, choices=[(s, s) for s in CreditState],
        default=CreditState.PENDING, db_index=True,
    )

    #: Unclaimed credits expire into the redistribution pool, which funds other
    #: users' drips. Without expiry the platform accrues an unbounded liability
    #: and users have no reason to come back.
    expires_at = models.DateTimeField(db_index=True)

    #: Set once resolved. May exceed or undercut amount_minor if the user
    #: gambled: this is what they actually received.
    settled_minor = AmountField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    #: Links to the games context by id only — no FK, no import.
    game_round_id = models.UUIDField(null=True, blank=True)

    reference_type = models.CharField(max_length=60, blank=True)
    reference_id = models.CharField(max_length=64, blank=True)

    objects = PendingCreditQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["user", "state", "expires_at"]),
            models.Index(fields=["state", "expires_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(amount_minor__gt=0), name="pending_credit_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.amount} to {self.user_id} ({self.state})"

    @property
    def amount(self) -> Amount:
        return Amount(self.amount_minor, Asset(self.asset))

    @property
    def is_claimable(self) -> bool:
        return self.state == CreditState.PENDING and self.expires_at > timezone.now()

    @property
    def source_label(self) -> str:
        return CreditSource(self.source).label


class Hold(BaseModel):
    """Funds reserved against something in flight.

    A lottery ticket, an open bid, a duel stake, a purchase awaiting delivery.
    The money leaves USER_AVAILABLE for USER_HELD so it cannot be double-spent,
    and comes back (release) or moves on (capture) exactly once.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="wallet_holds"
    )
    amount_minor = AmountField()
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)
    purpose = models.CharField(max_length=60)
    state = models.CharField(
        max_length=16, choices=[(s, s) for s in HoldState],
        default=HoldState.ACTIVE, db_index=True,
    )

    reference_type = models.CharField(max_length=60, blank=True, db_index=True)
    reference_id = models.CharField(max_length=64, blank=True, db_index=True)

    #: A hold with no expiry is a leak: a crashed sale would freeze the user's
    #: money forever. The sweeper releases anything past this.
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "state"]),
            models.Index(fields=["reference_type", "reference_id"]),
            models.Index(fields=["state", "expires_at"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(amount_minor__gt=0), name="hold_positive"),
        ]

    def __str__(self) -> str:
        return f"hold {self.amount} for {self.purpose}"

    @property
    def amount(self) -> Amount:
        return Amount(self.amount_minor, Asset(self.asset))


class LedgerAnomaly(BaseModel):
    """A recorded failure of an accounting invariant.

    Nothing writes here in normal operation. Rows in this table mean the
    reconciler found a discrepancy between materialised balances and postings,
    and they page a human. Keeping them in the database rather than only in logs
    means the evidence survives log rotation, which matters when the question is
    "where did the money go".
    """

    kind = models.CharField(max_length=60, db_index=True)
    account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.PROTECT, related_name="anomalies"
    )
    entry = models.ForeignKey(
        JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="anomalies"
    )
    detail = models.JSONField(default=dict)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "ledger anomalies"

    def __str__(self) -> str:
        return f"{self.kind} @ {self.created_at:%Y-%m-%d %H:%M}"


__all__ = [
    "Account",
    "AccountKind",
    "Hold",
    "JournalEntry",
    "LedgerAnomaly",
    "PendingCredit",
    "Posting",
    "SYSTEM_KINDS",
]

"""Who receives the 3%, how much was raised, and where it went.

The whole context is an accounting record with a public face. It holds no money:
the money sits in the ledger's CHARITY_POOL account, put there synchronously by
`wallet.services.recognise_revenue` at the moment each fee was taken. This app
records *whose* it is and whether it has been sent.

Nothing here may be edited casually. The transparency page is a promise, and a
promise you can retouch is a brochure.
"""

from __future__ import annotations

from django.db import models
from django.utils.text import slugify

from charity.constants import DEFAULT_WEIGHT, PayoutState
from core.models import BaseModel
from core.money import Amount, Asset

ASSET_CHOICES = [(a, a.label) for a in Asset]


class BeneficiaryQuerySet(models.QuerySet):
    def active(self) -> BeneficiaryQuerySet:
        return self.filter(is_active=True, weight__gt=0)


class Beneficiary(BaseModel):
    """An organisation that receives a share of the charity pool.

    `weight` is relative, not a percentage: percentages have to be kept summing
    to 100 by hand, and the day somebody adds a fifth beneficiary to a list of
    four 25s, the arithmetic silently stops adding up. Weights make the split a
    property of the set rather than a number to maintain.
    """

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(blank=True)

    #: Registro / codice fiscale of the organisation. Published, on purpose:
    #: it is what lets a sceptical user check that the charity is real.
    registration_number = models.CharField(max_length=60, blank=True)
    website = models.URLField(blank=True)

    weight = models.PositiveSmallIntegerField(
        default=DEFAULT_WEIGHT,
        help_text="Peso relativo nella ripartizione. 0 esclude dalla ripartizione.",
    )
    is_active = models.BooleanField(default=True, db_index=True)

    objects = BeneficiaryQuerySet.as_manager()

    class Meta:
        ordering = ("-weight", "name")
        verbose_name_plural = "beneficiaries"

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:140]
        return super().save(*args, **kwargs)


class CharityAllocation(BaseModel):
    """One `CharityAllocated` event, recorded.

    This is the *raised* number: the sum of every carve-out the ledger made, in
    the order it made them. It is deliberately independent of the payout side,
    so "how much have we set aside" and "how much have we sent" can be compared
    rather than assumed equal.

    `event_id` is unique so a redelivered event cannot inflate the total.
    """

    event_id = models.UUIDField(unique=True, null=True, blank=True)
    amount_minor = models.BigIntegerField()
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)
    basis_points = models.PositiveSmallIntegerField()
    #: Set only when a future event names the beneficiary up front. Today the
    #: contract carries None and the split happens at distribution time.
    beneficiary = models.ForeignKey(
        Beneficiary, on_delete=models.PROTECT, null=True, blank=True,
        related_name="allocations",
    )
    occurred_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ("-occurred_at",)
        indexes = [models.Index(fields=["asset", "occurred_at"])]

    def __str__(self) -> str:
        return f"{self.amount} → beneficenza"

    @property
    def amount(self) -> Amount:
        return Amount(self.amount_minor, Asset(self.asset))


class CharityPayoutQuerySet(models.QuerySet):
    def committed(self) -> CharityPayoutQuerySet:
        """Instructed or paid — i.e. already spoken for."""
        return self.filter(state__in=[PayoutState.INSTRUCTED, PayoutState.PAID])

    def paid(self) -> CharityPayoutQuerySet:
        return self.filter(state=PayoutState.PAID)

    def total_minor(self) -> int:
        return self.aggregate(total=models.Sum("amount_minor"))["total"] or 0


class CharityPayout(BaseModel):
    """A slice of the pool assigned to one beneficiary.

    Created in batches by `services.distribute_pool`. Every row in a batch shares
    a `batch` id and stores the weight that produced it, so a distribution can be
    re-derived years later without knowing what the weights are *now*.
    """

    beneficiary = models.ForeignKey(
        Beneficiary, on_delete=models.PROTECT, related_name="payouts"
    )
    amount_minor = models.BigIntegerField()
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)

    #: One distribution run. Rows sharing a batch sum to exactly the amount that
    #: was distributable at that moment.
    batch = models.UUIDField(db_index=True)
    weight_at_distribution = models.PositiveSmallIntegerField()

    state = models.CharField(
        max_length=12, choices=PayoutState.choices, default=PayoutState.INSTRUCTED,
        db_index=True,
    )
    #: Bank transfer id, receipt number — whatever proves it left.
    reference = models.CharField(max_length=120, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    objects = CharityPayoutQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["beneficiary", "state", "created_at"])]

    def __str__(self) -> str:
        return f"{self.amount} → {self.beneficiary.name} ({self.get_state_display()})"

    @property
    def amount(self) -> Amount:
        return Amount(self.amount_minor, Asset(self.asset))

"""What is for sale, what was bought, and where the money is while it moves.

The boundary shows up in the columns: `sale_id` is a plain UUID, because the
mechanic that sold this thing lives in `salemodes` and this context is not
allowed to know its schema. Everything the marketplace needs about a sale comes
back through `salemodes.services` or arrives as an event.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from core.models import AmountField, AmountMixin, BaseModel
from core.money import Amount, Asset
from marketplace.constants import (
    ESCROW_AUTO_RELEASE,
    SHIPPING_DEADLINE,
    DisputeState,
    ListingKind,
    ListingState,
    OrderState,
)

ASSET_CHOICES = [(a.value, a.label) for a in Asset]


class ListingQuerySet(models.QuerySet):
    def active(self):
        return self.filter(state=ListingState.ACTIVE)

    def of_kind(self, kind: str):
        return self.filter(kind=kind)

    def search(self, term: str):
        """Title and description, case-insensitively.

        Deliberately dumb. A real search is a later problem and pretending
        otherwise with a half-built ranking would be worse than an honest
        `icontains` that everybody can predict.
        """
        term = (term or "").strip()
        if not term:
            return self
        return self.filter(Q(title__icontains=term) | Q(description__icontains=term))


class Listing(BaseModel):
    """One thing offered for sale, under one mechanic.

    The mechanic is a registry key and a blob of validated config — this model
    knows nothing about lotteries. Adding a sale mode does not touch this table.
    """

    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="listings"
    )

    title = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, blank=True, db_index=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="listings/", blank=True)

    kind = models.CharField(
        max_length=16, choices=[(k, k.label) for k in ListingKind],
        default=ListingKind.PHYSICAL, db_index=True,
    )
    state = models.CharField(
        max_length=16, choices=[(s, s.label) for s in ListingState],
        default=ListingState.DRAFT, db_index=True,
    )

    #: What buyers pay in. A listing that pays its seller in euros must be
    #: denominated in the only biglia that can become euros again — enforced in
    #: `marketplace.services.create_listing`, not merely documented.
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES, default=Asset.BIGLIA_PIENA)

    #: The seller's payout choice. Drives the commission rate; see
    #: `marketplace.constants`.
    payout_in_biglie = models.BooleanField(default=True)

    #: Registry key + validated config. No FK, no choices: a new mode is a new
    #: string and nothing else.
    sale_mode = models.CharField(max_length=32, db_index=True)
    mode_config = models.JSONField(default=dict, blank=True)

    #: The sale currently disposing of this listing, by id.
    current_sale_id = models.UUIDField(null=True, blank=True, db_index=True)

    # -- physical goods -----------------------------------------------------
    ships_from = models.CharField(max_length=80, blank=True)
    shipping_note = models.CharField(max_length=200, blank=True)

    # -- digital goods ------------------------------------------------------
    #: Handed to the buyer on settlement. A key, a link, a code. Held here
    #: rather than emailed so that delivery is a database fact we can point at
    #: when somebody says they never got it.
    digital_payload = models.TextField(blank=True)

    # -- bets ---------------------------------------------------------------
    bet_question = models.CharField(max_length=200, blank=True)
    #: Where the answer comes from. A bet without a named source in advance is
    #: an argument waiting to happen.
    bet_resolution_source = models.CharField(max_length=200, blank=True)
    bet_resolves_at = models.DateTimeField(null=True, blank=True)
    bet_outcome = models.BooleanField(null=True, blank=True)
    bet_resolved_at = models.DateTimeField(null=True, blank=True)
    #: What the seller puts up against the buyer's stake, and the hold covering
    #: it. A proposition nobody covers is not a bet, it is a donation.
    bet_counter_stake_minor = AmountField(default=0)
    bet_cover_hold_id = models.UUIDField(null=True, blank=True)

    view_count = models.PositiveIntegerField(default=0)

    objects = ListingQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["state", "kind"]),
            models.Index(fields=["seller", "state"]),
            models.Index(fields=["sale_mode", "state"]),
        ]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)[:140] or str(uuid.uuid4())[:8]
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("marketplace:listing_detail", args=[self.pk])

    @property
    def listing_kind(self) -> ListingKind:
        return ListingKind(self.kind)

    @property
    def money_asset(self) -> Asset:
        return Asset(self.asset)

    @property
    def is_active(self) -> bool:
        return self.state == ListingState.ACTIVE

    @property
    def counter_stake(self) -> Amount:
        return Amount(self.bet_counter_stake_minor, self.money_asset)


class OrderQuerySet(models.QuerySet):
    def for_user(self, user):
        """Everything the user is a party to, either side of the transaction."""
        return self.filter(Q(buyer=user) | Q(seller=user))

    def open_escrows(self, now=None):
        now = now or timezone.now()
        return self.filter(
            state__in=(OrderState.ESCROW, OrderState.SHIPPED),
            escrow_release_at__isnull=False,
            escrow_release_at__lte=now,
        )


class Order(AmountMixin, BaseModel):
    """A concluded sale, and the escrow around it.

    `amount_minor` is the gross the buyer paid; `net_minor` is what the seller
    receives and `fee_minor` is the platform's cut. All three are stored rather
    than derived because the rate can change and an old order must still
    explain itself.
    """

    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name="orders")
    #: The sale this came out of. UUID, not FK: `salemodes` is a separate context.
    sale_id = models.UUIDField(db_index=True)

    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders"
    )
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sales"
    )

    amount_minor = AmountField()
    fee_minor = AmountField(default=0)
    net_minor = AmountField(default=0)
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)
    fee_basis_points = models.PositiveIntegerField(default=0)
    payout_in_biglie = models.BooleanField(default=True)

    state = models.CharField(
        max_length=16, choices=[(s, s.label) for s in OrderState],
        default=OrderState.ESCROW, db_index=True,
    )

    #: When escrow releases on its own. Cleared while a dispute is open — that
    #: is what "a dispute freezes the money" means mechanically.
    escrow_release_at = models.DateTimeField(null=True, blank=True, db_index=True)
    paid_out_at = models.DateTimeField(null=True, blank=True)
    #: The pending credit the seller was paid through. Sellers go through the
    #: claim funnel like everybody else.
    payout_credit_id = models.UUIDField(null=True, blank=True)

    #: What the mode decided beyond "you won": the prize drawn from a sealed
    #: box, the discount earned in a challenge.
    award_payload = models.JSONField(default=dict, blank=True)

    objects = OrderQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["buyer", "state"]),
            models.Index(fields=["seller", "state"]),
            models.Index(fields=["state", "escrow_release_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sale_id", "buyer"], name="uniq_order_per_sale_buyer"
            ),
        ]

    def __str__(self) -> str:
        return f"order {self.id} — {self.listing_id}"

    def get_absolute_url(self) -> str:
        return reverse("marketplace:order_detail", args=[self.pk])

    @property
    def order_state(self) -> OrderState:
        return OrderState(self.state)

    @property
    def gross(self) -> Amount:
        return Amount(self.amount_minor, Asset(self.asset))

    @property
    def fee(self) -> Amount:
        return Amount(self.fee_minor, Asset(self.asset))

    @property
    def net(self) -> Amount:
        return Amount(self.net_minor, Asset(self.asset))

    @property
    def is_frozen(self) -> bool:
        return self.state == OrderState.DISPUTED

    def default_escrow_release(self) -> timezone.datetime:
        return timezone.now() + (
            SHIPPING_DEADLINE if self.state == OrderState.ESCROW else ESCROW_AUTO_RELEASE
        )


class Delivery(BaseModel):
    """How the thing got to the buyer.

    One row per order, created with the order so that "nothing has happened
    yet" is a visible state rather than a missing row.
    """

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="delivery")

    #: Physical
    shipping_address = models.TextField(blank=True)
    carrier = models.CharField(max_length=60, blank=True)
    tracking_code = models.CharField(max_length=80, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)

    #: Digital — a copy of the listing payload taken at delivery time, so
    #: editing the listing afterwards cannot change what was handed over.
    payload = models.TextField(blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    #: The buyer's own confirmation. The thing that releases escrow early.
    confirmed_at = models.DateTimeField(null=True, blank=True)
    auto_released = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"delivery for {self.order_id}"

    @property
    def is_shipped(self) -> bool:
        return self.shipped_at is not None

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None


class Dispute(BaseModel):
    """A buyer says something is wrong. The money stops until a human says otherwise.

    Resolution is recorded with who decided and why: a dispute process whose
    reasoning is not written down is indistinguishable from a coin flip, and
    both parties can tell.
    """

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="disputes")
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="disputes_opened"
    )
    reason = models.TextField()
    state = models.CharField(
        max_length=20, choices=[(s, s.label) for s in DisputeState],
        default=DisputeState.OPEN, db_index=True,
    )

    resolution_note = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by_id = models.UUIDField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["state", "created_at"])]

    def __str__(self) -> str:
        return f"dispute on {self.order_id} ({self.state})"

    @property
    def is_open(self) -> bool:
        return self.state == DisputeState.OPEN

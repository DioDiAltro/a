"""State of a sale in flight.

Three tables and no more: the `Sale` (which mechanic, which window, which
money), the `Participation` (who put what in, and which wallet hold is
reserving it) and the `SaleOutcome` (what the draw decided and how to check it).

Note what is *not* here: no foreign key to a listing, no foreign key to an
order. `listing_id` and `seller_id` are bare UUIDs. `salemodes` must be liftable
out of this monolith without dragging the marketplace's schema behind it, and a
column with a REFERENCES clause is not a boundary, it is a promise to stay put.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.exceptions import IllegalStateTransition
from core.models import AmountField, AmountMixin, BaseModel
from core.money import Amount, Asset
from salemodes import registry
from salemodes.constants import (
    ALLOWED_TRANSITIONS,
    DEFAULT_EARLY_DRAW_NOTICE_MINUTES,
    ParticipationState,
    SaleState,
)

ASSET_CHOICES = [(a.value, a.label) for a in Asset]


class SaleQuerySet(models.QuerySet):
    def live(self):
        return self.filter(state__in=(SaleState.OPEN, SaleState.THRESHOLD_MET))

    def for_listing(self, listing_id):
        return self.filter(listing_id=listing_id)

    def due(self, now=None):
        """Sales the settler must act on.

        Two ways to become due, and the second one is an anti-abuse control: a
        deadline passed, or an announced early draw whose notice window has run
        out. The announcement is binding precisely because this query does not
        care whether the seller ever comes back.
        """
        now = now or timezone.now()
        return self.live().filter(
            Q(closes_at__isnull=False, closes_at__lte=now)
            | Q(settlement_due_at__isnull=False, settlement_due_at__lte=now)
        )


class Sale(BaseModel):
    """One thing being sold, one way of selling it."""

    #: The marketplace listing this sale disposes of. No FK: see module docstring.
    listing_id = models.UUIDField(db_index=True)
    seller_id = models.UUIDField(db_index=True)

    #: Registry key. Not a choices field: the whole point of the registry is
    #: that a new mechanic ships without a migration.
    mode = models.CharField(max_length=32, db_index=True)

    state = models.CharField(
        max_length=16, choices=[(s, s.label) for s in SaleState],
        default=SaleState.OPEN, db_index=True,
    )

    #: Mode-specific, validated by `SaleMode.validate_config` before it lands.
    config = models.JSONField(default=dict, blank=True)

    #: Everything in one sale is denominated in one asset. Mixing them would
    #: mean a pot that cannot be compared with its own threshold.
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES, default=Asset.BIGLIA_PIENA)

    #: The seller's choice, made at listing time. Biglie cost the seller a
    #: lower commission than euros — the rates live in `marketplace.constants`,
    #: which is where the fee policy belongs; we only carry the outcome.
    payout_in_biglie = models.BooleanField(default=True)
    fee_basis_points = models.PositiveIntegerField(default=0)

    opens_at = models.DateTimeField(default=timezone.now, db_index=True)
    closes_at = models.DateTimeField(null=True, blank=True, db_index=True)

    #: Running gross of the *active* participations. Denormalised because the
    #: threshold is checked on every ticket and the progress bar is on every
    #: page; kept honest by only ever being touched in `salemodes.holds`.
    total_minor = AmountField(default=0)
    units_total = models.PositiveIntegerField(default=0)

    threshold_reached_at = models.DateTimeField(null=True, blank=True)

    # -- early settlement, and the guards around it -------------------------

    #: When the seller announced an early draw. Public the moment it is set.
    settlement_announced_at = models.DateTimeField(null=True, blank=True)
    settlement_announced_by_id = models.UUIDField(null=True, blank=True)
    #: Announcement + notice window. The draw may not happen before this, and
    #: `settle_due_sales` makes sure it happens no later than it either.
    settlement_due_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # -- fairness -----------------------------------------------------------

    #: Published before the draw; the seed behind it is revealed after.
    draw_commitment_hash = models.CharField(max_length=64, blank=True)
    #: Secret until settlement. Never render this before `SaleOutcome` exists.
    draw_seed = models.CharField(max_length=128, blank=True)
    #: The `games` proof this draw was resolved through, when games backed it.
    draw_proof_id = models.UUIDField(null=True, blank=True)
    draw_source = models.CharField(max_length=16, blank=True)

    winner_id = models.UUIDField(null=True, blank=True, db_index=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    aborted_at = models.DateTimeField(null=True, blank=True)
    abort_reason = models.CharField(max_length=200, blank=True)

    metadata = models.JSONField(default=dict, blank=True)

    objects = SaleQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["state", "closes_at"]),
            models.Index(fields=["listing_id", "state"]),
            models.Index(fields=["seller_id", "state"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(total_minor__gte=0), name="sale_total_not_negative"),
            models.CheckConstraint(
                check=Q(fee_basis_points__lte=10000), name="sale_fee_within_range"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.mode} sale {self.id} ({self.state})"

    # -- money --------------------------------------------------------------

    @property
    def money_asset(self) -> Asset:
        return Asset(self.asset)

    @property
    def total(self) -> Amount:
        return Amount(self.total_minor, self.money_asset)

    def amount(self, minor: int) -> Amount:
        """Build an `Amount` in this sale's asset. Saves every mode a line."""
        return Amount(int(minor), self.money_asset)

    # -- mode ---------------------------------------------------------------

    @property
    def mode_instance(self):
        return registry.get(self.mode)

    @property
    def mode_label(self) -> str:
        return registry.get(self.mode).label if registry.has(self.mode) else self.mode

    @property
    def template_partial(self) -> str:
        """Where the listing page finds this mode's UI. The plugin seam, in HTML."""
        return registry.get(self.mode).template_partial

    # -- state machine ------------------------------------------------------

    @property
    def sale_state(self) -> SaleState:
        return SaleState(self.state)

    @property
    def is_open(self) -> bool:
        return self.sale_state.accepts_participation and not self.has_closed

    @property
    def has_closed(self) -> bool:
        return self.closes_at is not None and self.closes_at <= timezone.now()

    def transition_to(self, target: SaleState, *, save: bool = True) -> None:
        current = self.sale_state
        if target not in ALLOWED_TRANSITIONS[current]:
            raise IllegalStateTransition(current=str(current), attempted=str(target))
        self.state = target
        if save:
            self.save(update_fields=["state", "updated_at"])

    # -- early settlement ---------------------------------------------------

    @property
    def early_draw_notice(self) -> timedelta:
        minutes = int(
            self.config.get("early_draw_notice_minutes", DEFAULT_EARLY_DRAW_NOTICE_MINUTES)
        )
        return timedelta(minutes=minutes)

    @property
    def is_announced(self) -> bool:
        return self.settlement_announced_at is not None

    @property
    def notice_elapsed(self) -> bool:
        return self.settlement_due_at is not None and self.settlement_due_at <= timezone.now()

    @property
    def seconds_until_draw(self) -> int:
        if self.settlement_due_at is None:
            return 0
        return max(0, int((self.settlement_due_at - timezone.now()).total_seconds()))

    # -- reads --------------------------------------------------------------

    def active_participations(self):
        return self.participations.filter(state=ParticipationState.ACTIVE).order_by("created_at")

    @property
    def participant_count(self) -> int:
        return (
            self.participations.filter(state=ParticipationState.ACTIVE)
            .values("user_id")
            .distinct()
            .count()
        )

    def participation_of(self, user):
        return self.participations.filter(
            user=user, state=ParticipationState.ACTIVE
        ).order_by("-created_at").first()


class ParticipationQuerySet(models.QuerySet):
    def active(self):
        return self.filter(state=ParticipationState.ACTIVE)


class Participation(AmountMixin, BaseModel):
    """Somebody's stake in a sale: a ticket, a bid, a slot, a sealed box.

    `hold_id` is the wallet's reservation, referenced by id because
    `wallet.models` is not ours to import. Every path out of ACTIVE either
    captures that hold or releases it — there is no third option, which is why
    a participant's money can never be stranded.
    """

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="participations")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sale_participations"
    )

    amount_minor = AmountField()
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)
    #: Tickets, slots, boxes. Weight of this entry in a draw.
    units = models.PositiveIntegerField(default=1)

    #: The hold covering the seller's share of this participation.
    hold_id = models.UUIDField(null=True, blank=True, db_index=True)
    #: The hold covering the platform commission. Split at hold time because a
    #: single hold can only be captured to a single account — see
    #: `salemodes.holds` for the full reasoning.
    fee_hold_id = models.UUIDField(null=True, blank=True)
    fee_minor = AmountField(default=0)

    state = models.CharField(
        max_length=16, choices=[(s, s.label) for s in ParticipationState],
        default=ParticipationState.ACTIVE, db_index=True,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = ParticipationQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["sale", "state"]),
            models.Index(fields=["user", "state"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(amount_minor__gt=0), name="participation_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} in {self.sale_id}: {self.amount}"

    @property
    def principal(self) -> Amount:
        """What reaches escrow — the amount less the commission."""
        return Amount(self.amount_minor - self.fee_minor, Asset(self.asset))

    @property
    def fee(self) -> Amount:
        return Amount(self.fee_minor, Asset(self.asset))


class SaleOutcome(BaseModel):
    """What the sale decided, and everything needed to check it.

    Written inside the settlement transaction. `verification` carries the
    revealed seed and the exact entry list the draw ran over, so a suspicious
    participant can replay the winner themselves without asking us for
    anything.
    """

    sale = models.OneToOneField(Sale, on_delete=models.CASCADE, related_name="outcome")

    winner_id = models.UUIDField(null=True, blank=True, db_index=True)
    gross_minor = AmountField(default=0)
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)

    #: `games` proof id when the draw ran through the games context.
    fairness_proof_id = models.UUIDField(null=True, blank=True)

    #: One entry per buyer: [{"user_id": ..., "gross_minor": ..., "payload": {}}]
    awards = models.JSONField(default=list, blank=True)
    verification = models.JSONField(default=dict, blank=True)

    aborted = models.BooleanField(default=False)
    reason = models.CharField(max_length=200, blank=True)

    def __str__(self) -> str:
        return f"outcome of {self.sale_id}"

    @property
    def gross(self) -> Amount:
        return Amount(self.gross_minor, Asset(self.asset))

    @property
    def award_count(self) -> int:
        return len(self.awards)

    def award_payload(self, user_id) -> dict[str, Any]:
        for award in self.awards:
            if str(award.get("user_id")) == str(user_id):
                return award.get("payload") or {}
        return {}

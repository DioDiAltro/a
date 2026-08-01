"""The sale context's public API — the state machine around the plugins.

`marketplace` calls these functions and subscribes to the events they publish.
It never touches our models and never asks which mode is running: this module
owns the parts that are the same for every mechanic (the window, the state
machine, the compliance gate, the events) and delegates everything that differs
to the `SaleMode` instance.

Where the events go, and why
----------------------------
`SaleSettled` is published with `publish_now`: the marketplace's handler creates
the order and pays the seller, and those writes must commit with the settlement
or not at all. A settled sale that failed to produce an order would be a prize
nobody can claim.

Everything else — opened, participation, threshold, aborted — goes through
`publish`, so it is delivered after commit. A notification that fails must never
be able to unwind a draw.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from compliance.constants import LimitKind
from compliance.services import assert_can_play, check_limit, record_usage
from core.contracts import (
    MARKETPLACE_FEE_BASIS_POINTS,
    SaleAborted,
    SaleOpened,
    SaleParticipation,
    SaleSettled,
    SaleThresholdReached,
)
from core.events import bus
from core.exceptions import IllegalStateTransition, SaleModeError
from core.money import Amount, Asset, allocate
from salemodes import fairness, registry
from salemodes.base import ParticipationResult, SettlementResult
from salemodes.constants import ParticipationState, SaleState
from salemodes.events import EarlyDrawAnnounced, ParticipationRefused
from salemodes.models import Participation, Sale, SaleOutcome

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


def validate_config(mode_key: str, config: dict[str, Any]) -> dict[str, Any]:
    """Normalise a mode's configuration or refuse it, in Italian.

    Called by the listing form *before* anything is created, so a bad config is
    a red field rather than a sale that cannot settle.
    """
    return registry.get(mode_key).validate_config(dict(config or {}))


@transaction.atomic
def open_sale(
    *,
    listing_id: UUID,
    seller_id: UUID,
    mode_key: str,
    config: dict[str, Any],
    asset: Asset = Asset.BIGLIA_PIENA,
    payout_in_biglie: bool = True,
    fee_basis_points: int = MARKETPLACE_FEE_BASIS_POINTS,
    opens_at: datetime | None = None,
    closes_at: datetime | None = None,
) -> Sale:
    """Put a listing on sale under one mechanic.

    The config is validated here as well as in the form: a sale opened by an
    API client, a fixture or a management command must meet the same bar.
    """
    mode = registry.get(mode_key)
    normalised = mode.validate_config(dict(config or {}))
    opens_at = opens_at or timezone.now()

    sale = Sale(
        listing_id=listing_id,
        seller_id=seller_id,
        mode=mode_key,
        config=normalised,
        asset=asset,
        payout_in_biglie=payout_in_biglie,
        fee_basis_points=fee_basis_points,
        opens_at=opens_at,
        closes_at=closes_at or mode.initial_closes_at(normalised, opens_at),
    )

    decision = mode.can_start(sale)
    if not decision:
        raise SaleModeError(decision.reason)

    if mode.draws_outcome:
        _commit_to_a_draw(sale)

    sale.save()

    bus.publish(
        SaleOpened(
            sale_id=sale.id, listing_id=listing_id, seller_id=seller_id,
            mode=mode_key, closes_at=sale.closes_at,
        )
    )
    logger.info("sale=%s opened mode=%s closes_at=%s", sale.pk, mode_key, sale.closes_at)
    return sale


def _commit_to_a_draw(sale: Sale) -> None:
    """Publish a commitment before the sale can know who will enter it."""
    commitment = fairness.open_commitment(purpose="sale_draw", reference_id=str(sale.pk))
    sale.draw_commitment_hash = commitment.hash
    sale.draw_seed = commitment.seed
    sale.draw_proof_id = commitment.proof_id
    sale.draw_source = commitment.source


# ---------------------------------------------------------------------------
# Participating
# ---------------------------------------------------------------------------


@transaction.atomic
def participate(*, sale: Sale, user, amount: Amount, units: int = 1) -> ParticipationResult:
    """Buy a ticket, place a bid, take a slot, open a box, play the challenge.

    Locks the sale row: two people buying the last box, or the last ticket
    before a draw, must be serialised by the database rather than by hope.
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)
    mode = sale.mode_instance

    _assert_open(sale)
    _assert_eligible(sale, user, mode)

    if mode.draws_outcome:
        # A ticket in a drawn sale is a wager: it goes through the same
        # self-exclusion gate and the same self-imposed limits as a minigame.
        check_limit(user, LimitKind.WAGER, amount)

    result = mode.on_participation(sale, user, amount, units)

    if not result.accepted:
        bus.publish(
            ParticipationRefused(sale_id=sale.id, user_id=user.id, reason=result.message)
        )
        return result

    if mode.draws_outcome:
        record_usage(user, LimitKind.WAGER, amount, reference=f"sale:{sale.pk}")

    bus.publish(
        SaleParticipation(
            sale_id=sale.id, user_id=user.id, amount_minor=amount.minor,
            asset=sale.money_asset, units=units,
        )
    )

    if result.threshold_reached and sale.sale_state is SaleState.OPEN:
        _mark_threshold_reached(sale)

    if result.settle_now:
        settle_sale(sale, early=True, enforce_early_guard=False)

    return result


def _assert_open(sale: Sale) -> None:
    if not sale.sale_state.accepts_participation:
        raise IllegalStateTransition(current=sale.state, attempted="participate")
    now = timezone.now()
    if sale.opens_at > now:
        raise SaleModeError("Questa vendita non è ancora aperta.")
    if sale.closes_at is not None and sale.closes_at <= now:
        raise SaleModeError("Questa vendita è chiusa.")


def _assert_eligible(sale: Sale, user, mode) -> None:
    """The gates that apply to every mechanic.

    The self-participation rule is first because it is the one with teeth: a
    seller buying into their own lottery can meet their own minimum, inflate a
    pot they are paid from, and hold a ticket in a draw they choose the moment
    of. Barring it removes all three at once.
    """
    if not mode.allows_seller_participation and str(user.pk) == str(sale.seller_id):
        raise SaleModeError(
            "Non puoi partecipare a una vendita tua. Il minimo lo devono fare gli altri."
        )

    assert_can_play(user)

    if not mode.allows_repeat_participation and sale.participations.filter(
        user=user, state="active"
    ).exists():
        raise SaleModeError("Hai già partecipato a questa vendita.")


def _mark_threshold_reached(sale: Sale) -> None:
    sale.threshold_reached_at = timezone.now()
    sale.transition_to(SaleState.THRESHOLD_MET, save=False)
    sale.save(update_fields=["state", "threshold_reached_at", "updated_at"])

    threshold = int(sale.config.get("minimum_minor") or sale.config.get("min_buyers") or 0)
    bus.publish(
        SaleThresholdReached(
            sale_id=sale.id, seller_id=sale.seller_id, total_minor=sale.total_minor,
            threshold_minor=threshold, asset=sale.money_asset,
        )
    )
    logger.info("sale=%s threshold reached total=%s", sale.pk, sale.total_minor)


# ---------------------------------------------------------------------------
# Announcing an early settlement — the anti-abuse control
# ---------------------------------------------------------------------------


@transaction.atomic
def announce_early_settlement(*, sale: Sale, actor_id: UUID) -> Sale:
    """Start the notice window before an early draw.

    Three properties make this an anti-abuse control rather than a formality:

    * it may only be made once the mode says the sale is eligible (for a
      lottery: the minimum is in);
    * it is **binding** — `settle_due_sales` runs the draw at `settlement_due_at`
      whether or not the seller returns, so announcing and cancelling until the
      ticket list looks favourable is not available;
    * it **re-commits** the draw seed. The commitment is published before the
      final entry list exists, so even the platform cannot pick a moment that
      suits a particular ticket.
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if str(actor_id) != str(sale.seller_id):
        raise SaleModeError("Solo il venditore può annunciare la chiusura anticipata.")

    if sale.is_announced:
        # Idempotent, and deliberately does not push the window out: a seller
        # who could re-announce could shop for a moment one window at a time.
        return sale

    sale.mode_instance.can_announce_settlement(sale).raise_if_denied()

    now = timezone.now()
    sale.settlement_announced_at = now
    sale.settlement_announced_by_id = actor_id
    sale.settlement_due_at = now + sale.early_draw_notice

    if sale.mode_instance.draws_outcome:
        _commit_to_a_draw(sale)

    sale.save(
        update_fields=[
            "settlement_announced_at", "settlement_announced_by_id", "settlement_due_at",
            "draw_commitment_hash", "draw_seed", "draw_proof_id", "draw_source",
            "updated_at",
        ]
    )

    bus.publish(
        EarlyDrawAnnounced(
            sale_id=sale.id, listing_id=sale.listing_id, seller_id=sale.seller_id,
            draw_at=sale.settlement_due_at, total_minor=sale.total_minor,
            asset=sale.money_asset, commitment_hash=sale.draw_commitment_hash,
        )
    )
    logger.info("sale=%s early draw announced for %s", sale.pk, sale.settlement_due_at)
    return sale


# ---------------------------------------------------------------------------
# Settling
# ---------------------------------------------------------------------------


@transaction.atomic
def settle_sale(sale: Sale, *, early: bool = False, actor_id: UUID | None = None,
                enforce_early_guard: bool = True) -> SaleOutcome:
    """Close a sale: decide the outcome, capture the money, publish the result.

    Idempotent: a second call on a settled sale returns the outcome the first
    one wrote rather than paying anybody twice.
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    if sale.sale_state.is_terminal:
        existing = SaleOutcome.objects.filter(sale=sale).first()
        if existing is not None:
            return existing
        raise IllegalStateTransition(current=sale.state, attempted="settle")

    if actor_id is not None and str(actor_id) != str(sale.seller_id):
        raise SaleModeError("Solo il venditore può chiudere questa vendita.")

    mode = sale.mode_instance

    if early and enforce_early_guard:
        mode.can_settle_early(sale).raise_if_denied()

    sale.transition_to(SaleState.SETTLING)
    result = mode.settle(sale)

    if result.aborted:
        return abort_sale(sale, reason=result.reason)

    return _record_settlement(sale, result)


def _record_settlement(sale: Sale, result: SettlementResult) -> SaleOutcome:
    gross_total = sum(award.gross.minor for award in result.awards)
    principals, fees = _apportion_captured(sale, result)

    outcome = SaleOutcome.objects.create(
        sale=sale,
        winner_id=result.winner_id,
        gross_minor=gross_total,
        asset=sale.asset,
        fairness_proof_id=result.fairness_proof_id,
        awards=[
            {
                "user_id": str(award.user_id),
                "gross_minor": award.gross.minor,
                #: What is actually sitting in ESCROW for this award, and what
                #: is actually sitting in HOUSE_REVENUE. The marketplace pays
                #: these exact figures out rather than recomputing a
                #: percentage, which is what keeps escrow at exactly zero.
                "net_minor": principals[index].minor,
                "fee_minor": fees[index].minor,
                "payload": award.payload,
            }
            for index, award in enumerate(result.awards)
        ],
        # The reveal. Publishing the seed alongside the exact entries and
        # weights is what turns "trust us" into "check it yourself".
        verification=result.payload.get("verification", {}),
    )

    sale.state = SaleState.SETTLED
    sale.winner_id = result.winner_id
    sale.settled_at = timezone.now()
    sale.save(update_fields=["state", "winner_id", "settled_at", "updated_at"])

    # One event per buyer: a group buy settles to everybody who joined, and the
    # order engine should not have to know that some modes have many winners.
    for award in result.awards:
        bus.publish_now(
            SaleSettled(
                sale_id=sale.id,
                listing_id=sale.listing_id,
                seller_id=sale.seller_id,
                winner_id=award.user_id,
                gross_minor=award.gross.minor,
                asset=sale.money_asset,
                payout_in_biglie=sale.payout_in_biglie,
                fairness_proof_id=result.fairness_proof_id,
            )
        )

    logger.info(
        "sale=%s settled awards=%s gross=%s", sale.pk, len(result.awards), gross_total
    )
    return outcome


def _apportion_captured(sale: Sale, result: SettlementResult) -> tuple[list[Amount], list[Amount]]:
    """Split what was really captured across the awards.

    A lottery captures a hundred tickets and produces one award; a group buy
    captures ten slots and produces ten. Either way the money in ESCROW and the
    commission in HOUSE_REVENUE have to be attributed to orders, and the parts
    must sum to exactly what was captured — so this uses `allocate`, which
    hands out the remainder minor units deterministically rather than letting
    each order round independently and losing a scaglia per buyer.
    """
    captured = sale.participations.filter(state=ParticipationState.CAPTURED)
    principal_total = sum(p.amount_minor - p.fee_minor for p in captured)
    fee_total = sum(p.fee_minor for p in captured)

    weights = [max(1, award.gross.minor) for award in result.awards]
    if not weights:
        return [], []

    return (
        allocate(Amount(principal_total, sale.money_asset), weights),
        allocate(Amount(fee_total, sale.money_asset), weights),
    )


@transaction.atomic
def abort_sale(sale: Sale, *, reason: str) -> SaleOutcome:
    """Refund everybody and close the sale.

    The refund runs through the mode so a mechanic with an unusual settlement
    can have an unusual refund, but every mode in this package uses the default:
    release every hold, which returns the exact minor units reserved.
    """
    sale = Sale.objects.select_for_update().get(pk=sale.pk)

    existing = SaleOutcome.objects.filter(sale=sale).first()
    if existing is not None:
        return existing

    refund = sale.mode_instance.refund(sale)

    sale.state = SaleState.ABORTED
    sale.aborted_at = timezone.now()
    sale.abort_reason = reason[:200]
    sale.total_minor = 0
    sale.units_total = 0
    sale.save(
        update_fields=["state", "aborted_at", "abort_reason", "total_minor",
                       "units_total", "updated_at"]
    )

    outcome = SaleOutcome.objects.create(
        sale=sale, asset=sale.asset, aborted=True, reason=reason[:200],
        verification={"refunded_count": refund.refunded_count,
                      "refunded_minor": refund.total_minor},
    )

    bus.publish(SaleAborted(sale_id=sale.id, reason=reason[:200]))
    logger.info(
        "sale=%s aborted reason=%r refunded=%s", sale.pk, reason, refund.total_minor
    )
    return outcome


def settle_due(now: datetime | None = None) -> list[SaleOutcome]:
    """Settle or abort everything past its deadline or its announced draw time.

    Each sale gets its own transaction: one mode raising must not strand the
    others, and a lottery that cannot settle is a support ticket, not a reason
    to stop settling everybody else's.
    """
    outcomes: list[SaleOutcome] = []

    for sale in Sale.objects.due(now).order_by("closes_at", "settlement_due_at"):
        try:
            outcomes.append(settle_sale(sale, early=sale.is_announced,
                                        enforce_early_guard=False))
        except Exception:
            logger.exception("Failed to settle sale %s", sale.pk)

    return outcomes


# ---------------------------------------------------------------------------
# Reads — everything `marketplace` needs without touching our models
# ---------------------------------------------------------------------------


def get_sale(sale_id) -> Sale | None:
    return Sale.objects.filter(pk=sale_id).first()


def active_sale_for_listing(listing_id) -> Sale | None:
    return Sale.objects.for_listing(listing_id).live().order_by("-created_at").first()


def latest_sale_for_listing(listing_id) -> Sale | None:
    return Sale.objects.for_listing(listing_id).order_by("-created_at").first()


def sales_for_seller(seller_id, *, limit: int = 50) -> list[Sale]:
    return list(Sale.objects.filter(seller_id=seller_id).order_by("-created_at")[:limit])


def participations_of(user, *, limit: int = 50) -> list[Participation]:
    return list(
        Participation.objects.filter(user=user)
        .select_related("sale")
        .order_by("-created_at")[:limit]
    )


def outcome_for(sale_id) -> SaleOutcome | None:
    return SaleOutcome.objects.filter(sale_id=sale_id).first()


def award_for(sale_id, user_id) -> dict[str, Any] | None:
    """One buyer's share of a settlement: gross, net, commission, mode payload.

    The marketplace asks this instead of recomputing the fee from a percentage.
    The sale knows what it actually captured; a second party recomputing the
    same figure from a rate is how escrow ends up a scaglia short.
    """
    outcome = outcome_for(sale_id)
    if outcome is None:
        return None
    for award in outcome.awards:
        if str(award.get("user_id")) == str(user_id):
            return award
    return None


def sale_context(sale: Sale, *, user=None) -> dict[str, Any]:
    """Everything a template needs to render a sale, mode included.

    The listing page includes `template_partial` blindly. That is the plugin
    seam expressed in HTML: a new mechanic ships its own interface and the
    marketplace's templates do not learn a new `{% if %}`.
    """
    mode = sale.mode_instance
    context: dict[str, Any] = {
        "sale": sale,
        "mode": mode,
        "mode_key": mode.key,
        "mode_label": mode.label,
        "mode_description": mode.description,
        "partial": mode.template_partial,
        "progress": mode.progress(sale),
        "summary": mode.summary(sale),
        "state": sale.sale_state,
        "is_open": sale.is_open,
        "total": sale.total,
        "participant_count": sale.participant_count,
        "closes_at": sale.closes_at,
        "commitment_hash": sale.draw_commitment_hash,
    }

    if user is not None and getattr(user, "is_authenticated", False):
        is_seller = str(user.pk) == str(sale.seller_id)
        context["is_seller"] = is_seller
        context["my_participation"] = sale.participation_of(user)
        if is_seller:
            context["can_announce"] = mode.can_announce_settlement(sale)
            context["can_settle_early"] = mode.can_settle_early(sale)

    return context

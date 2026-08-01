"""The only place a sale touches money.

Every mode reaches for these helpers instead of calling the wallet itself, so
"which sale mechanic can move a balance, and how" has a single, short answer.

Why each participation carries *two* holds
------------------------------------------
`wallet.services.capture_hold` moves a whole hold to a single account. The
seller's share must land in ESCROW and the platform's commission must land in
HOUSE_REVENUE, and there is no wallet primitive that splits a captured hold or
moves value between two system accounts. So the split happens where it *can* be
exact — at hold time — and settlement is then two clean captures.

The user experiences one payment: the two holds always sum to what they agreed
to pay, and both are released together if the sale falls through.

Keeping the parts exact
-----------------------
For a pooled sale (a lottery pot, a group buy) the commission is apportioned
**marginally**: each ticket's fee is the fee on the pot including it, minus the
fee on the pot without it. Rounding down at each step and summing gives exactly
the fee on the final pot — the property the settlement test asserts to the
scaglia. Taking 5% of each ticket independently would not: it drifts by a
scaglia per ticket, and those scaglie would have to come from somewhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.money import Amount, split_percentage
from salemodes.base import RefundResult
from salemodes.constants import (
    FEE_HOLD_PURPOSE,
    HOLD_GRACE_DAYS,
    HOLD_PURPOSE,
    FeeScope,
    ParticipationState,
)
from salemodes.models import Participation, Sale
from wallet.constants import AccountKind, EntryKind
from wallet.services import capture_hold, place_hold, release_hold

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HoldRef:
    """A wallet hold, referred to by id.

    `release_hold` and `capture_hold` both re-read the hold under a row lock and
    only ever read `.pk` off what they are given, so a context that may not
    import `wallet.models` can still resolve a hold it placed earlier. The day
    the wallet grows `capture_hold_by_id(uuid)` this class disappears; until
    then this is the seam, stated openly rather than smuggled in as an import.
    """

    pk: UUID


# ---------------------------------------------------------------------------
# Commission
# ---------------------------------------------------------------------------


def fee_for(sale: Sale, amount: Amount, *, scope: FeeScope) -> Amount:
    """The commission carried by a participation of `amount`.

    POOL scope apportions marginally against the running pot; WINNER scope takes
    the fee on the participation alone, because only one of them will be
    captured and the rest go back untouched.
    """
    if not sale.fee_basis_points:
        return Amount.zero(amount.asset)

    if scope is FeeScope.WINNER:
        fee, _ = split_percentage(amount, sale.fee_basis_points)
        return fee

    before, _ = split_percentage(sale.total, sale.fee_basis_points)
    after, _ = split_percentage(sale.total + amount, sale.fee_basis_points)
    return after - before


def _hold_expiry(sale: Sale) -> timedelta:
    """How long the reservation outlives the sale.

    Never open-ended: a settlement that crashes must not freeze somebody's
    money until a human notices. `wallet.tasks.release_expired_holds` sweeps
    anything past this and the participant gets their scaglie back.
    """
    grace = timedelta(days=HOLD_GRACE_DAYS)
    if sale.closes_at is None:
        return grace
    remaining = sale.closes_at - timezone.now()
    return max(remaining, timedelta(0)) + grace


# ---------------------------------------------------------------------------
# Placing
# ---------------------------------------------------------------------------


@transaction.atomic
def record_participation(sale: Sale, user, amount: Amount, *, units: int = 1,
                         metadata: dict | None = None) -> Participation:
    """Reserve the money and write the participation row.

    Raises `InsufficientFunds` from the wallet if the user cannot cover it —
    deliberately not caught here, because a mode has nothing useful to add to
    "non hai abbastanza biglie" and swallowing it would let a sale record a
    participation nobody paid for.
    """
    mode = sale.mode_instance
    fee = fee_for(sale, amount, scope=mode.fee_scope)
    principal = amount - fee
    expires_in = _hold_expiry(sale)

    principal_hold = place_hold(
        user=user,
        amount=principal,
        purpose=HOLD_PURPOSE,
        reference_type="sale",
        reference_id=str(sale.pk),
        expires_in=expires_in,
    )
    fee_hold = None
    if fee.is_positive:
        fee_hold = place_hold(
            user=user,
            amount=fee,
            purpose=FEE_HOLD_PURPOSE,
            reference_type="sale_fee",
            reference_id=str(sale.pk),
            expires_in=expires_in,
        )

    participation = Participation.objects.create(
        sale=sale,
        user=user,
        amount_minor=amount.minor,
        asset=amount.asset,
        units=units,
        hold_id=principal_hold.pk,
        fee_hold_id=fee_hold.pk if fee_hold else None,
        fee_minor=fee.minor,
        metadata=metadata or {},
    )

    Sale.objects.filter(pk=sale.pk).update(
        total_minor=F("total_minor") + amount.minor,
        units_total=F("units_total") + units,
    )
    sale.total_minor += amount.minor
    sale.units_total += units
    return participation


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def _hold_refs(participation: Participation) -> list[HoldRef]:
    refs = []
    if participation.hold_id:
        refs.append(HoldRef(participation.hold_id))
    if participation.fee_hold_id:
        refs.append(HoldRef(participation.fee_hold_id))
    return refs


@transaction.atomic
def release(participation: Participation, *, reason: str = "") -> Amount:
    """Give one participant their money back, to the scaglia.

    Uses `release_hold`, which posts the held amount back to the available
    balance — the same integer that left it. A refund computed as a percentage
    of anything would be a refund that can be wrong.
    """
    if participation.state != ParticipationState.ACTIVE:
        return Amount.zero(participation.amount.asset)

    for ref in _hold_refs(participation):
        release_hold(ref)

    participation.state = ParticipationState.RELEASED
    participation.resolved_at = timezone.now()
    if reason:
        participation.metadata = {**participation.metadata, "release_reason": reason}
    participation.save(update_fields=["state", "resolved_at", "metadata", "updated_at"])

    Sale.objects.filter(pk=participation.sale_id).update(
        total_minor=F("total_minor") - participation.amount_minor,
        units_total=F("units_total") - participation.units,
    )
    return participation.amount


def release_all(sale: Sale, *, reason: str = "") -> RefundResult:
    """Release every active participation. The abort path, and the default refund."""
    count = 0
    total = 0
    for participation in sale.active_participations().select_for_update():
        given_back = release(participation, reason=reason)
        count += 1
        total += given_back.minor

    logger.info("sale=%s refunded participations=%s total=%s", sale.pk, count, total)
    return RefundResult(refunded_count=count, total_minor=total, reason=reason)


@transaction.atomic
def capture(participation: Participation) -> Amount:
    """Consume a participation: principal to escrow, commission to the house.

    Returns the gross the participant paid. The commission does not linger in
    escrow — it goes straight to HOUSE_REVENUE, where `recognise_revenue` can
    carve out charity and redistribution from a balance that actually exists.
    """
    if participation.state != ParticipationState.ACTIVE:
        return Amount.zero(participation.amount.asset)

    if participation.hold_id:
        capture_hold(
            HoldRef(participation.hold_id),
            to=AccountKind.ESCROW,
            kind=EntryKind.PURCHASE,
        )
    if participation.fee_hold_id:
        capture_hold(
            HoldRef(participation.fee_hold_id),
            to=AccountKind.HOUSE_REVENUE,
            kind=EntryKind.FEE,
        )

    participation.state = ParticipationState.CAPTURED
    participation.resolved_at = timezone.now()
    participation.save(update_fields=["state", "resolved_at", "updated_at"])
    return participation.amount


def capture_all(sale: Sale) -> Amount:
    """Capture every active participation. The pooled-settlement path."""
    total = Amount.zero(sale.money_asset)
    for participation in sale.active_participations().select_for_update():
        total = total + capture(participation)
    return total

"""The charity context's public API.

Two jobs: keep the running record of what was set aside, and split the pool
across beneficiaries without losing a scaglia.

The split uses `core.money.allocate`, which hands the remainder minor units to
the largest weights in a defined order. That matters more than it looks: a pool
of 1001 scaglie across three equal charities cannot be divided evenly, and the
choice is between a documented rule and a stray unit that quietly accumulates in
a rounding account forever.
"""

from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from charity.constants import COMMITTED_STATES, MIN_DISTRIBUTION_MINOR, PayoutState
from charity.models import Beneficiary, CharityAllocation, CharityPayout
from core.contracts import CharityAllocated
from core.exceptions import IllegalStateTransition, InvalidAmount
from core.money import Amount, Asset, allocate
from wallet.selectors import charity_pool_balances

logger = logging.getLogger(__name__)


def _aware(moment):
    """Normalise an event timestamp.

    `DomainEvent.occurred_at` defaults to `datetime.now()`, which is naive, and
    this project runs with `USE_TZ = True`. Storing it raw would warn and then
    quietly reinterpret the value; converting once, here, keeps the record
    honest.
    """
    if moment is None:
        return timezone.now()
    if timezone.is_naive(moment):
        return timezone.make_aware(moment, timezone.get_default_timezone())
    return moment


# ---------------------------------------------------------------------------
# The running record
# ---------------------------------------------------------------------------


def record_allocation(event: CharityAllocated) -> CharityAllocation | None:
    """Persist a `CharityAllocated` event.

    Idempotent on `event_id`: the bus is in-process today and a broker with
    at-least-once delivery is the explicit future, so a redelivered event must
    not be able to inflate the public total.
    """
    if event.amount_minor <= 0:
        return None

    allocation, created = CharityAllocation.objects.get_or_create(
        event_id=event.event_id,
        defaults={
            "amount_minor": event.amount_minor,
            "asset": str(event.asset),
            "basis_points": event.basis_points,
            "beneficiary_id": event.beneficiary_id,
            "occurred_at": _aware(event.occurred_at),
        },
    )
    if not created:
        logger.info("Ignored redelivered CharityAllocated %s", event.event_id)
    return allocation


def total_raised() -> dict[Asset, Amount]:
    """Everything ever carved out for charity, per asset.

    This is the cumulative figure and it only ever grows. The *pool balance* —
    what is still sitting there waiting to be sent — is a different number, and
    the transparency page shows both, because showing only the first would let
    "raised" drift into meaning "given".
    """
    rows = CharityAllocation.objects.values("asset").annotate(total=Sum("amount_minor"))
    return {Asset(row["asset"]): Amount(row["total"] or 0, Asset(row["asset"])) for row in rows}


def total_paid() -> dict[Asset, Amount]:
    rows = (
        CharityPayout.objects.paid()
        .values("asset")
        .annotate(total=Sum("amount_minor"))
    )
    return {Asset(row["asset"]): Amount(row["total"] or 0, Asset(row["asset"])) for row in rows}


def committed_minor(asset: Asset) -> int:
    """Pool money already assigned to a beneficiary, instructed or paid."""
    return (
        CharityPayout.objects.filter(asset=str(asset), state__in=COMMITTED_STATES)
        .aggregate(total=Sum("amount_minor"))["total"]
        or 0
    )


def distributable(asset: Asset) -> Amount:
    """What a distribution run may hand out right now.

    The pool balance minus what previous runs already spoke for. Without the
    subtraction, running the job twice before the transfers are made would
    promise the same scaglie to two different charities.
    """
    pool = charity_pool_balances().get(asset, Amount.zero(asset))
    return Amount(max(0, pool.minor - committed_minor(asset)), asset)


# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------


@transaction.atomic
def distribute_pool(*, asset: Asset | None = None, force: bool = False) -> list[CharityPayout]:
    """Split the distributable pool across active beneficiaries by weight.

    Returns the payouts created, which sum to *exactly* the distributable
    amount: `allocate()` guarantees no minor unit is invented or lost, and this
    function asserts it rather than trusting it, because a charity total that
    does not tie out is the one number on this platform nobody would forgive.

    Passing no `asset` distributes every asset that has something to give.
    """
    if asset is None:
        created: list[CharityPayout] = []
        for candidate in charity_pool_balances():
            created.extend(distribute_pool(asset=candidate, force=force))
        return created

    amount = distributable(asset)
    if not amount.is_positive:
        return []
    if not force and amount.minor < MIN_DISTRIBUTION_MINOR:
        logger.info(
            "Charity distribution skipped: %s available, minimum is %s minor units",
            amount, MIN_DISTRIBUTION_MINOR,
        )
        return []

    beneficiaries = list(Beneficiary.objects.active())
    if not beneficiaries:
        logger.warning(
            "Charity pool holds %s but no active beneficiary is configured. "
            "The money stays in the pool until somebody is named.", amount,
        )
        return []

    weights = [b.weight for b in beneficiaries]
    shares = allocate(amount, weights)

    batch = uuid.uuid4()
    payouts = [
        CharityPayout(
            beneficiary=beneficiary,
            amount_minor=share.minor,
            asset=str(asset),
            batch=batch,
            weight_at_distribution=beneficiary.weight,
            state=PayoutState.INSTRUCTED,
        )
        for beneficiary, share in zip(beneficiaries, shares, strict=True)
    ]

    total = sum(p.amount_minor for p in payouts)
    if total != amount.minor:
        # Unreachable while `allocate` keeps its contract. Checked anyway: this
        # is the assertion that turns a silent rounding leak into a rollback.
        raise InvalidAmount(
            f"Charity split does not tie out: {total} distributed against "
            f"{amount.minor} available."
        )

    CharityPayout.objects.bulk_create(payouts)
    logger.info(
        "Charity distribution %s: %s split across %d beneficiaries",
        batch, amount, len(payouts),
    )
    return payouts


@transaction.atomic
def confirm_payout(payout: CharityPayout, *, reference: str) -> CharityPayout:
    """Mark a payout as actually sent, with the proof.

    `reference` is required. A payout that claims to be paid without a transfer
    reference is exactly the row an auditor would ask about, and "we forgot to
    fill it in" is not an answer.
    """
    if not reference.strip():
        raise InvalidAmount("A confirmed payout needs a transfer reference.")

    payout = CharityPayout.objects.select_for_update().get(pk=payout.pk)
    if payout.state != PayoutState.INSTRUCTED:
        raise IllegalStateTransition(current=payout.state, attempted=PayoutState.PAID)

    payout.state = PayoutState.PAID
    payout.reference = reference.strip()
    payout.paid_at = timezone.now()
    payout.save(update_fields=["state", "reference", "paid_at", "updated_at"])
    return payout


@transaction.atomic
def fail_payout(payout: CharityPayout, *, reason: str) -> CharityPayout:
    """The transfer bounced. The slice returns to the distributable pool."""
    payout = CharityPayout.objects.select_for_update().get(pk=payout.pk)
    if payout.state != PayoutState.INSTRUCTED:
        raise IllegalStateTransition(current=payout.state, attempted=PayoutState.FAILED)

    payout.state = PayoutState.FAILED
    payout.note = reason
    payout.save(update_fields=["state", "note", "updated_at"])
    return payout

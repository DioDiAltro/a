"""Read side of the charity context — everything the public page needs.

Kept separate from `services` so that the transparency page cannot accidentally
call something that writes, and so `web` has one obvious place to read from.
"""

from __future__ import annotations

from django.db.models import Q, Sum

from charity.constants import COMMITTED_STATES, PayoutState
from charity.models import Beneficiary, CharityAllocation, CharityPayout
from charity.services import distributable, total_paid, total_raised
from core.contracts import CHARITY_BASIS_POINTS
from core.money import Amount, Asset
from wallet.selectors import charity_pool_balances


def active_beneficiaries() -> list[Beneficiary]:
    return list(Beneficiary.objects.active())


def beneficiary_breakdown(asset: Asset = Asset.BIGLIA_PIENA) -> list[dict]:
    """Per-beneficiary totals plus the share each would receive next time.

    The projected share is computed from the live weights rather than stored,
    so the page cannot show a split that no longer matches what the next
    distribution would actually do.
    """
    beneficiaries = list(
        Beneficiary.objects.filter(Q(is_active=True) | Q(payouts__isnull=False))
        .distinct()
        .annotate(
            paid_minor=Sum(
                "payouts__amount_minor",
                filter=Q(payouts__state=PayoutState.PAID, payouts__asset=str(asset)),
            ),
            committed_minor=Sum(
                "payouts__amount_minor",
                filter=Q(payouts__state__in=COMMITTED_STATES, payouts__asset=str(asset)),
            ),
        )
    )

    total_weight = sum(b.weight for b in beneficiaries if b.is_active and b.weight > 0)

    rows = []
    for beneficiary in beneficiaries:
        rows.append(
            {
                "beneficiary": beneficiary,
                "paid": Amount(beneficiary.paid_minor or 0, asset),
                "committed": Amount(beneficiary.committed_minor or 0, asset),
                "share_percent": (
                    round(beneficiary.weight * 100 / total_weight, 1)
                    if beneficiary.is_active and total_weight
                    else 0.0
                ),
            }
        )
    rows.sort(key=lambda row: (-row["paid"].minor, -row["share_percent"]))
    return rows


def recent_payouts(*, limit: int = 20) -> list[CharityPayout]:
    return list(
        CharityPayout.objects.select_related("beneficiary")
        .exclude(state=PayoutState.CANCELLED)[:limit]
    )


def recent_allocations(*, limit: int = 10) -> list[CharityAllocation]:
    return list(CharityAllocation.objects.all()[:limit])


def transparency_summary(asset: Asset = Asset.BIGLIA_PIENA) -> dict:
    """One call for the whole public page.

    Three numbers that must be shown together, because any one of them alone is
    misleading: what was set aside, what is still sitting in the pool, and what
    has actually left for a bank account.
    """
    raised = total_raised()
    paid = total_paid()
    pools = charity_pool_balances()

    return {
        "asset": asset,
        "raised": raised.get(asset, Amount.zero(asset)),
        "paid": paid.get(asset, Amount.zero(asset)),
        "in_pool": pools.get(asset, Amount.zero(asset)),
        "awaiting_transfer": Amount(
            max(
                0,
                (
                    CharityPayout.objects.filter(
                        asset=str(asset), state=PayoutState.INSTRUCTED
                    ).aggregate(total=Sum("amount_minor"))["total"]
                    or 0
                ),
            ),
            asset,
        ),
        "distributable": distributable(asset),
        "raised_by_asset": raised,
        "pools": pools,
        "beneficiaries": beneficiary_breakdown(asset),
        "payouts": recent_payouts(),
        "allocations": recent_allocations(),
        "basis_points": CHARITY_BASIS_POINTS,
        "percent": CHARITY_BASIS_POINTS / 100,
    }

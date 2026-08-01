"""Is the currency sound?

`docs/ECONOMY.md` is the normative answer: only a confirmed euro deposit mints
biglie, every other source moves value that already existed, and the sinks must
out-weigh the sources or the biglia inflates and the platform dies of arithmetic
rather than of a bug.

This module is the instrument that would notice. It is deliberately built from
events this context already records rather than from the ledger: `economy` may
not import `wallet.ledger`, and — more importantly — a watchdog that reads the
same source as the thing it is watching cannot detect a disagreement.

One honest limitation, stated in the numbers rather than hidden: `core.contracts`
has `WithdrawalRequested` but no completion event, so burns are known only as
requests. Everything derived from them is labelled as a bound, not a fact.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from core.money import Amount, Asset
from economy.constants import (
    BANKROLL_BLEED_TOLERANCE_BASIS_POINTS,
    DRIP_ASSET,
    INFLATION_WINDOW,
    MIN_REVENUE_FOR_ALERT_MINOR,
)
from economy.models import (
    DripAward,
    EconomySnapshot,
    GameRoundOutcome,
    MintRecord,
    RevenueRecord,
    WithdrawalRecord,
)
from wallet.selectors import charity_pool_balances, redistribution_pool_balance

logger = logging.getLogger("economy")


# ---------------------------------------------------------------------------
# Supply
# ---------------------------------------------------------------------------


def minted_total() -> Amount:
    """Every biglia ever created, from the only event that creates them.

    Deposits mint BIGLIA_PIENA and nothing else does, so this is exact for
    issuance even though it is assembled from events.
    """
    total = MintRecord.objects.aggregate(total=Sum("biglie_minor"))["total"] or 0
    return Amount(total, Asset.BIGLIA_PIENA)


def withdrawal_requested_total() -> Amount:
    """Euros users have asked to take out. A *lower bound* on what has left."""
    total = WithdrawalRecord.objects.aggregate(total=Sum("euro_minor"))["total"] or 0
    return Amount(total, Asset.EUR)


def circulating_supply() -> dict:
    """Best available picture of biglie in circulation.

    Not exact, and the return value says so. The ledger knows precisely — its
    ISSUANCE account balance *is* the figure — but that lives behind
    `wallet.ledger`, which this context may not import. Until `wallet.selectors`
    exposes it, or a `WithdrawalCompleted` event exists, this is an upper bound
    on outstanding supply.
    """
    minted = minted_total()
    requested_out = withdrawal_requested_total()
    return {
        "minted": minted,
        "withdrawal_requested": requested_out,
        "upper_bound": Amount(max(0, minted.minor - requested_out.minor), Asset.BIGLIA_PIENA),
        "is_exact": False,
        "why": (
            "Il libro mastro conosce la cifra esatta (saldo del conto ISSUANCE); "
            "economy non può leggerlo senza violare i confini fra contesti, e il "
            "contratto non ha un evento di prelievo completato."
        ),
    }


# ---------------------------------------------------------------------------
# Sources against sinks
# ---------------------------------------------------------------------------


def sources_and_sinks(*, window: timedelta | None = None, now=None) -> dict:
    """What flowed toward players, and what flowed back to the house.

    Sources are the three ways value reaches a user: minting (deposits), the
    house bankroll paying out a winning round, and the redistribution drip.
    Only the first is new money — which is the entire thesis of `ECONOMY.md`,
    and the reason the three are reported apart rather than as one figure.
    """
    now = now or timezone.now()
    window = window or INFLATION_WINDOW
    start = now - window

    minted = (
        MintRecord.objects.filter(occurred_at__gte=start).aggregate(t=Sum("biglie_minor"))["t"]
        or 0
    )

    revenue_rows = RevenueRecord.objects.filter(occurred_at__gte=start).aggregate(
        total=Sum("amount_minor"),
        charity=Sum("charity_minor"),
        redistribution=Sum("redistribution_minor"),
    )

    drip_total = (
        DripAward.objects.filter(awarded_at__gte=start).aggregate(t=Sum("amount_minor"))["t"] or 0
    )

    rounds = GameRoundOutcome.objects.filter(settled_at__gte=start).aggregate(
        count=Count("id"), staked=Sum("stake_minor"), paid=Sum("payout_minor")
    )
    staked = rounds["staked"] or 0
    paid = rounds["paid"] or 0
    # What the bankroll handed over beyond the stakes it took in. Negative means
    # the house came out ahead over the window, which is the normal case.
    house_paid = max(0, paid - staked)

    revenue = revenue_rows["total"] or 0

    return {
        "window_hours": int(window.total_seconds() // 3600),
        "start": start,
        "end": now,
        "sources": {
            "minted_minor": minted,
            "house_paid_minor": house_paid,
            "drip_minor": drip_total,
            "total_minor": minted + house_paid + drip_total,
        },
        "sinks": {
            "revenue_minor": revenue,
            "of_which_charity_minor": revenue_rows["charity"] or 0,
            "of_which_redistribution_minor": revenue_rows["redistribution"] or 0,
            "total_minor": revenue,
        },
        "games": {
            "rounds": rounds["count"] or 0,
            "staked_minor": staked,
            "paid_minor": paid,
            "realised_rtp_bp": (paid * 10_000 // staked) if staked else None,
        },
        "net_to_users_minor": minted + house_paid + drip_total - revenue,
        "pools": {
            "charity": charity_pool_balances(),
            "redistribution": redistribution_pool_balance(DRIP_ASSET),
        },
    }


def inflation_alert(*, window: timedelta | None = None, now=None) -> dict | None:
    """The alarm. Returns the most serious problem found, or None.

    Three things can go wrong, in descending order of how much they should ruin
    somebody's afternoon:

    1. **The drip outran its funding.** It is supposed to redistribute at most
       the share of revenue that feeds the pool. If it paid more than that over
       a window, either the caps are wrong or something is paying drips outside
       this context — and a drip that is not redistribution is issuance.
    2. **The bankroll is bleeding.** Winnings are paid out of accumulated house
       edge. Over a full window, payouts materially exceeding revenue means an
       outcome table is wrong, and it compounds.
    3. **Nothing is draining.** Sources without sinks is the failure mode
       `ECONOMY.md` is written to prevent.
    """
    figures = sources_and_sinks(window=window, now=now)
    sources = figures["sources"]
    sinks = figures["sinks"]

    drip = sources["drip_minor"]
    funding = sinks["of_which_redistribution_minor"]
    if drip > 0 and drip > funding:
        return _alert(
            "drip_outran_funding",
            "Il drip ha distribuito più di quanto i ricavi abbiano versato nel pool.",
            figures,
            drip_minor=drip,
            funding_minor=funding,
        )

    revenue = sinks["revenue_minor"]
    if revenue >= MIN_REVENUE_FOR_ALERT_MINOR:
        ceiling = revenue * BANKROLL_BLEED_TOLERANCE_BASIS_POINTS // 10_000
        if sources["house_paid_minor"] > ceiling:
            return _alert(
                "bankroll_bleed",
                "La casa ha pagato più di quanto ha incassato: una tabella dei premi "
                "è probabilmente sbagliata.",
                figures,
                house_paid_minor=sources["house_paid_minor"],
                revenue_minor=revenue,
            )

    if sources["minted_minor"] > 0 and revenue == 0:
        return _alert(
            "no_sinks",
            "Sono state coniate biglie e non è stato riconosciuto alcun ricavo nella "
            "finestra: un'economia di sole fonti si gonfia.",
            figures,
            minted_minor=sources["minted_minor"],
        )

    return None


def _alert(code: str, message: str, figures: dict, **detail) -> dict:
    logger.error("Economy alert %s: %s (%s)", code, message, detail)
    return {"code": code, "message": message, "detail": detail, "figures": figures}


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def take_snapshot(*, window: timedelta | None = None, now=None) -> EconomySnapshot:
    """Write one row describing the shape of the economy right now."""
    now = now or timezone.now()
    figures = sources_and_sinks(window=window, now=now)
    alert = inflation_alert(window=window, now=now)

    charity_pools = figures["pools"]["charity"]
    charity_pool_minor = sum(amount.minor for amount in charity_pools.values())

    return EconomySnapshot.objects.create(
        taken_at=now,
        window_hours=figures["window_hours"],
        minted_minor=figures["sources"]["minted_minor"],
        house_paid_minor=figures["sources"]["house_paid_minor"],
        drip_minor=figures["sources"]["drip_minor"],
        revenue_minor=figures["sinks"]["revenue_minor"],
        charity_minor=figures["sinks"]["of_which_charity_minor"],
        redistribution_minor=figures["sinks"]["of_which_redistribution_minor"],
        charity_pool_minor=charity_pool_minor,
        redistribution_pool_minor=figures["pools"]["redistribution"].minor,
        alert=alert["code"] if alert else "",
        detail={
            "games": figures["games"],
            "net_to_users_minor": figures["net_to_users_minor"],
            "alert": {k: v for k, v in (alert or {}).items() if k != "figures"},
        },
    )

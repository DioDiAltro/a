"""Read-side API of the wallet context.

`services` moves money; `selectors` answers questions. Both are public; the
models, the ledger and the constants module behind them are not.

Presentation code reaches for this module. Keeping the queries here rather than
letting views build their own means a change to how balances are stored is a
change to one file, and it stops a template from accidentally triggering a scan
of the postings table on every page load.
"""

from __future__ import annotations

import uuid

from core.money import Amount, Asset
from wallet.constants import AccountKind
from wallet.ledger import system_account
from wallet.models import PendingCredit


def claimable_credits(user, *, limit: int = 50) -> list[PendingCredit]:
    """The user's unclaimed credits, soonest to expire first.

    Ordered by expiry rather than by amount or recency on purpose: the one about
    to be lost is the one that deserves the top of the page.
    """
    return list(PendingCredit.objects.claimable(user)[:limit])


def get_claimable_credit(user, credit_id: uuid.UUID) -> PendingCredit | None:
    """Fetch one credit, scoped to its owner.

    Scoped by user in the query itself rather than fetched-then-checked, so
    there is no path where a caller forgets the ownership test.
    """
    return PendingCredit.objects.filter(pk=credit_id, user=user).first()


def charity_pool_balances() -> dict[Asset, Amount]:
    """Live balance of the charity pool, per asset.

    Read straight off the ledger. The public transparency page shows this
    number, and a cached counter that could drift from the books would make the
    whole feature marketing rather than accounting.
    """
    return {
        asset: system_account(AccountKind.CHARITY_POOL, asset).balance for asset in Asset
    }


def redistribution_pool_balance(asset: Asset) -> Amount:
    """What the passive drip has left to give. Zero means no drip today."""
    return system_account(AccountKind.REDISTRIBUTION_POOL, asset).balance

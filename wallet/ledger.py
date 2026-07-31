"""The posting engine — the only code in the platform that moves value.

Every other module asks this one. It refuses anything that would leave the books
unbalanced, and it takes locks in a fixed order so concurrent settlements cannot
deadlock each other.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from django.db import transaction
from django.db.models import F, Sum

from core.exceptions import InsufficientFunds, LedgerImbalance
from core.money import Amount, Asset
from wallet.constants import AccountKind, EntryKind
from wallet.models import Account, JournalEntry, LedgerAnomaly, Posting

logger = logging.getLogger("wallet.ledger")


@dataclass(frozen=True, slots=True)
class Leg:
    """One side of a movement. Negative debits the account, positive credits it."""

    account: Account
    amount: Amount

    def __post_init__(self) -> None:
        if self.amount.minor == 0:
            raise LedgerImbalance("A zero-value leg is meaningless; omit it.")
        if self.account.asset != self.amount.asset:
            raise LedgerImbalance(
                f"Leg posts {self.amount.asset} into a {self.account.asset} account "
                f"({self.account}). Accounts hold exactly one asset."
            )


def system_account(kind: AccountKind, asset: Asset) -> Account:
    return Account.objects.system(kind, asset)


def user_account(user, kind: AccountKind, asset: Asset) -> Account:
    account, _ = Account.objects.get_or_create(
        owner=user, kind=kind, asset=asset, defaults={"balance_minor": 0}
    )
    return account


def _assert_balanced(legs: list[Leg]) -> None:
    """Per-asset sums must be zero.

    Per *asset*, not overall — a deposit legitimately touches euros and biglie in
    one entry, and each side balances independently. Summing across assets would
    let a bug net €5 against 5 biglie and call it square.
    """
    totals: dict[Asset, int] = defaultdict(int)
    for leg in legs:
        totals[leg.amount.asset] += leg.amount.minor

    unbalanced = {asset: total for asset, total in totals.items() if total != 0}
    if unbalanced:
        raise LedgerImbalance(
            "Postings do not sum to zero per asset: "
            + ", ".join(f"{a.value} off by {t}" for a, t in unbalanced.items())
        )


@transaction.atomic
def post(
    *,
    kind: EntryKind,
    legs: list[Leg],
    description: str = "",
    reference_type: str = "",
    reference_id: str = "",
    idempotency_key: str = "",
    metadata: dict | None = None,
    allow_overdraft: bool = False,
) -> JournalEntry:
    """Write one balanced journal entry and move the balances.

    Raises `LedgerImbalance` if the legs don't net to zero, and
    `InsufficientFunds` if a user account would go negative. Both roll the whole
    thing back — there is no partial posting.
    """
    if not legs:
        raise LedgerImbalance("An entry needs at least two legs; got none.")

    _assert_balanced(legs)

    # Collapse repeated hits on the same account so we lock each row once and
    # a single entry can't both credit and debit its way past a balance check.
    net: dict[str, int] = defaultdict(int)
    accounts_by_id: dict[str, Account] = {}
    for leg in legs:
        net[str(leg.account.id)] += leg.amount.minor
        accounts_by_id[str(leg.account.id)] = leg.account

    # Fixed lock order (by primary key) across every caller. Two settlements
    # touching the same pair of accounts therefore queue instead of deadlocking.
    locked = {
        str(a.id): a
        for a in Account.objects.select_for_update().filter(id__in=accounts_by_id).order_by("id")
    }

    for account_id, delta in net.items():
        account = locked[account_id]
        if account.owner_id is not None and not allow_overdraft:
            new_balance = account.balance_minor + delta
            if new_balance < 0:
                raise InsufficientFunds(
                    required=Amount(-delta, Asset(account.asset)),
                    available=Amount(account.balance_minor, Asset(account.asset)),
                    message=(
                        f"Account {account} holds {account.balance_minor} and the entry "
                        f"would take it to {new_balance}."
                    ),
                )

    entry = JournalEntry.objects.create(
        kind=kind,
        description=description,
        reference_type=reference_type,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        metadata=metadata or {},
    )

    Posting.objects.bulk_create(
        [
            Posting(
                entry=entry,
                account=leg.account,
                amount_minor=leg.amount.minor,
                asset=leg.amount.asset,
            )
            for leg in legs
        ]
    )

    # F() so the write is a single atomic UPDATE against the locked row rather
    # than a read-modify-write that could clobber a concurrent change.
    for account_id, delta in net.items():
        Account.objects.filter(id=account_id).update(balance_minor=F("balance_minor") + delta)

    logger.info(
        "entry=%s kind=%s ref=%s:%s legs=%s",
        entry.id, kind, reference_type, reference_id,
        [(str(leg.account), leg.amount.minor) for leg in legs],
    )
    return entry


def balance_of(user, asset: Asset, kind: AccountKind = AccountKind.USER_AVAILABLE) -> Amount:
    account = (
        Account.objects.filter(owner=user, kind=kind, asset=asset)
        .values_list("balance_minor", flat=True)
        .first()
    )
    return Amount(account or 0, asset)


def spendable_total(user) -> dict[Asset, Amount]:
    """Available balance per biglia asset. Euros are not spendable in-market."""
    rows = Account.objects.filter(
        owner=user, kind=AccountKind.USER_AVAILABLE
    ).values_list("asset", "balance_minor")
    balances = {a: Amount.zero(a) for a in Asset if a.is_biglia}
    for asset, minor in rows:
        asset = Asset(asset)
        if asset.is_biglia:
            balances[asset] = Amount(minor, asset)
    return balances


def circulating_supply(asset: Asset) -> Amount:
    """Biglie in existence: the magnitude of the issuance account's debt."""
    issuance = system_account(AccountKind.ISSUANCE, asset)
    return Amount(-issuance.balance_minor, asset)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile(*, record_anomalies: bool = True) -> list[dict]:
    """Prove the materialised balances still equal the sum of the postings.

    Run in CI and on a schedule. A non-empty result means the ledger is lying
    and the platform should stop taking money until a human has looked.
    """
    problems: list[dict] = []

    posted = {
        row["account"]: row["total"]
        for row in Posting.objects.values("account").annotate(total=Sum("amount_minor"))
    }

    for account in Account.objects.iterator():
        expected = posted.get(account.id, 0)
        if account.balance_minor != expected:
            problem = {
                "kind": "balance_drift",
                "account_id": str(account.id),
                "account": str(account),
                "materialised": account.balance_minor,
                "posted": expected,
                "delta": account.balance_minor - expected,
            }
            problems.append(problem)
            if record_anomalies:
                LedgerAnomaly.objects.create(
                    kind="balance_drift", account=account, detail=problem
                )

    # Global per-asset closure: with issuance modelled as a negative account,
    # every asset's postings must sum to exactly zero across the whole ledger.
    for row in Posting.objects.values("asset").annotate(total=Sum("amount_minor")):
        if row["total"] != 0:
            problem = {
                "kind": "asset_not_closed",
                "asset": row["asset"],
                "total": row["total"],
            }
            problems.append(problem)
            if record_anomalies:
                LedgerAnomaly.objects.create(kind="asset_not_closed", detail=problem)

    if problems:
        logger.error("Ledger reconciliation found %d problem(s)", len(problems))

    return problems

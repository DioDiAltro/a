"""The wallet context's public API.

Other contexts import from this module and nothing else in `wallet`. Every
function here is idempotent when given a key, transactional, and publishes the
events in `core.contracts` that the rest of the platform reacts to.

A note on where winnings come from, because it is the question that decides
whether the economy survives: game payouts are settled against HOUSE_REVENUE,
never minted from ISSUANCE. A player who doubles their credit is paid out of the
house's accumulated edge, which may dip negative on a lucky streak and recovers
by arithmetic. The only thing in the entire platform that mints biglie is a
confirmed euro deposit. That single rule is what keeps the currency from
inflating no matter how generous the gamification gets.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.contracts import (
    CHARITY_BASIS_POINTS,
    PENDING_CREDIT_TTL_DAYS,
    REDISTRIBUTION_BASIS_POINTS,
    SAFE_CLAIM_FEE_BASIS_POINTS,
    CharityAllocated,
    CreditClaimed,
    CreditPending,
    DripAwarded,
    FundsTransferred,
    HoldPlaced,
    RevenueRecognised,
)
from core.events import bus
from core.exceptions import (
    AssetNotTransferable,
    IllegalStateTransition,
    InsufficientFunds,
    InvalidAmount,
)
from core.idempotency import idempotent
from core.money import Amount, Asset, split_percentage
from wallet.constants import AccountKind, CreditSource, CreditState, EntryKind, HoldState
from wallet.ledger import Leg, post, spendable_total, system_account, user_account
from wallet.models import Hold, PendingCredit

logger = logging.getLogger("wallet.ledger")


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


def summary_for(user) -> dict:
    """Everything the chrome needs about a user's money, in one query set.

    `unclaimed_count` is the number the whole product hangs on — it is the badge
    that pulls people back to the dashboard, where the claim ritual lives.
    """
    balances = spendable_total(user)
    pending = PendingCredit.objects.claimable(user)
    pending_by_asset: dict[Asset, Amount] = {a: Amount.zero(a) for a in balances}
    unclaimed = 0

    for credit in pending.only("amount_minor", "asset"):
        asset = Asset(credit.asset)
        pending_by_asset[asset] = pending_by_asset.get(asset, Amount.zero(asset)) + Amount(
            credit.amount_minor, asset
        )
        unclaimed += 1

    total_available = sum((b.minor for b in balances.values()), 0)
    total_pending = sum((p.minor for p in pending_by_asset.values()), 0)

    return {
        "balances": balances,
        "pending": pending_by_asset,
        "unclaimed_count": unclaimed,
        # Presented as one headline figure. Summing across biglia types is fine
        # here and only here: it is a display convenience, not arithmetic the
        # ledger relies on, and the breakdown is one click away.
        "total_available_minor": total_available,
        "total_pending_minor": total_pending,
        "scaglie_to_next": (total_available % 100) if total_available else 0,
    }


# ---------------------------------------------------------------------------
# Issuance — the only mint
# ---------------------------------------------------------------------------


@transaction.atomic
def issue_for_deposit(*, user, euro: Amount, biglie: Amount, idempotency_key: str,
                      reference_id: str = "") -> PendingCredit:
    """Turn confirmed euros into biglie. The single inflationary path, on purpose.

    Called only by `payments` after a settled payment. The euros are recorded
    against EXTERNAL_FIAT so the books tie back to the bank, and the biglie are
    issued against ISSUANCE whose negative balance is our liability to users.

    The biglie land as a *pending credit* like everything else: even a top-up
    goes through the claim ritual, so the user's very first interaction with
    their money is a choice.
    """
    if euro.asset is not Asset.EUR:
        raise InvalidAmount("issue_for_deposit takes euros on the fiat leg.")
    if biglie.asset is not Asset.BIGLIA_PIENA:
        raise InvalidAmount("Deposits issue BIGLIA_PIENA — the only withdrawable biglia.")
    if not euro.is_positive or not biglie.is_positive:
        raise InvalidAmount("Deposit amounts must be positive.")

    with idempotent(idempotency_key, scope="wallet.issue", payload={"ref": reference_id}) as h:
        if h.replayed:
            return PendingCredit.objects.get(pk=h.result_ref)

        post(
            kind=EntryKind.DEPOSIT,
            legs=[
                # Fiat leg: money left the outside world, we now hold it.
                Leg(system_account(AccountKind.EXTERNAL_FIAT, Asset.EUR), -euro),
                Leg(system_account(AccountKind.HOUSE_REVENUE, Asset.EUR), euro),
                # Biglia leg: new supply, parked for the user to claim.
                Leg(system_account(AccountKind.ISSUANCE, Asset.BIGLIA_PIENA), -biglie),
                Leg(user_account(user, AccountKind.USER_PENDING, Asset.BIGLIA_PIENA), biglie),
            ],
            description=f"Deposit {euro} → {biglie}",
            reference_type="deposit",
            reference_id=reference_id,
            idempotency_key=idempotency_key,
        )

        credit = _record_pending(
            user=user, amount=biglie, source=CreditSource.DEPOSIT,
            reference_type="deposit", reference_id=reference_id,
        )
        h.result_ref = str(credit.pk)
        return credit


@transaction.atomic
def burn_for_withdrawal(*, user, biglie: Amount, euro: Amount, idempotency_key: str,
                        reference_id: str = "") -> None:
    """Destroy withdrawable biglie and release euros. Mirror of issuance.

    Callers must have already checked KYC and limits — `payments` owns that
    policy, the ledger owns the arithmetic.
    """
    if biglie.asset is not Asset.BIGLIA_PIENA:
        raise InvalidAmount(
            f"{biglie.asset.label} cannot be withdrawn. Only biglie bought with "
            "real money leave as real money."
        )

    with idempotent(idempotency_key, scope="wallet.burn", payload={"ref": reference_id}) as h:
        if h.replayed:
            return

        post(
            kind=EntryKind.WITHDRAWAL,
            legs=[
                Leg(user_account(user, AccountKind.USER_AVAILABLE, Asset.BIGLIA_PIENA), -biglie),
                Leg(system_account(AccountKind.ISSUANCE, Asset.BIGLIA_PIENA), biglie),
                Leg(system_account(AccountKind.HOUSE_REVENUE, Asset.EUR), -euro),
                Leg(system_account(AccountKind.EXTERNAL_FIAT, Asset.EUR), euro),
            ],
            description=f"Withdrawal {biglie} → {euro}",
            reference_type="withdrawal",
            reference_id=reference_id,
            idempotency_key=idempotency_key,
        )


# ---------------------------------------------------------------------------
# Pending credits — the universal ingress
# ---------------------------------------------------------------------------


def _record_pending(*, user, amount: Amount, source: CreditSource,
                    reference_type: str = "", reference_id: str = "",
                    ttl_days: int = PENDING_CREDIT_TTL_DAYS) -> PendingCredit:
    credit = PendingCredit.objects.create(
        user=user,
        amount_minor=amount.minor,
        asset=amount.asset,
        source=source,
        expires_at=timezone.now() + timedelta(days=ttl_days),
        reference_type=reference_type,
        reference_id=reference_id,
    )
    bus.publish(
        CreditPending(
            credit_id=credit.id, user_id=user.id, amount_minor=amount.minor,
            asset=amount.asset, source=str(source), expires_at=credit.expires_at,
        )
    )
    return credit


@transaction.atomic
def credit_pending(*, user, amount: Amount, source: CreditSource, idempotency_key: str,
                   from_account: AccountKind = AccountKind.ESCROW,
                   reference_type: str = "", reference_id: str = "") -> PendingCredit:
    """Park value for a user, awaiting their claim.

    This is how every context hands value to a user: lottery prizes, duel
    winnings, sale proceeds, refunds, gifts. There is deliberately no
    `credit_available()` — bypassing the claim would bypass the product.
    """
    if not amount.is_positive:
        raise InvalidAmount("Cannot credit a non-positive amount.")

    with idempotent(idempotency_key, scope="wallet.credit_pending",
                    payload={"ref": reference_id, "amt": amount.minor}) as h:
        if h.replayed:
            return PendingCredit.objects.get(pk=h.result_ref)

        post(
            kind=EntryKind.PENDING_CREDIT,
            legs=[
                Leg(system_account(from_account, amount.asset), -amount),
                Leg(user_account(user, AccountKind.USER_PENDING, amount.asset), amount),
            ],
            description=f"{source} → pending",
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            allow_overdraft=True,  # system source accounts may run negative
        )

        credit = _record_pending(
            user=user, amount=amount, source=source,
            reference_type=reference_type, reference_id=reference_id,
        )
        h.result_ref = str(credit.pk)
        return credit


@transaction.atomic
def claim_safe(*, credit: PendingCredit, idempotency_key: str) -> Amount:
    """Take the credit without risking it, less a small fee.

    Returns what actually landed in the available balance.
    """
    credit = _lock_claimable(credit)

    fee, net = split_percentage(credit.amount, SAFE_CLAIM_FEE_BASIS_POINTS)

    with idempotent(idempotency_key, scope="wallet.claim_safe",
                    payload={"credit": str(credit.pk)}) as h:
        if h.replayed:
            return Amount(credit.settled_minor or 0, Asset(credit.asset))

        legs = [
            Leg(user_account(credit.user, AccountKind.USER_PENDING, credit.amount.asset),
                -credit.amount),
            Leg(user_account(credit.user, AccountKind.USER_AVAILABLE, credit.amount.asset),
                net),
        ]
        if fee:
            legs.append(Leg(system_account(AccountKind.HOUSE_REVENUE, fee.asset), fee))

        post(
            kind=EntryKind.CLAIM_SAFE,
            legs=legs,
            description=f"Safe claim of {credit.amount}",
            reference_type="pending_credit",
            reference_id=str(credit.pk),
            idempotency_key=idempotency_key,
        )

        credit.state = CreditState.CLAIMED
        credit.settled_minor = net.minor
        credit.settled_at = timezone.now()
        credit.save(update_fields=["state", "settled_minor", "settled_at", "updated_at"])

        if fee:
            recognise_revenue(amount=fee, source="safe_claim_fee", reference_id=credit.pk)

        bus.publish(
            CreditClaimed(
                credit_id=credit.id, user_id=credit.user_id, settled_minor=net.minor,
                original_minor=credit.amount_minor, asset=credit.amount.asset, risked=False,
            )
        )
        h.result_ref = str(credit.pk)
        return net


@transaction.atomic
def settle_risked_claim(*, credit: PendingCredit, payout: Amount, game_round_id,
                        idempotency_key: str) -> Amount:
    """Apply a minigame's verdict to a gambled credit.

    The `games` context decides `payout` — this function only moves money, so
    the ledger stays honest even if a game is buggy: the stake always leaves,
    the payout always comes from the house, and the difference is recognised as
    revenue (or absorbed as a loss on a lucky round).

    `payout` of zero is legitimate and means the user lost it.
    """
    credit = _lock_claimable(credit)

    if payout.asset is not credit.amount.asset:
        raise InvalidAmount("A gamble must pay out in the asset that was staked.")
    if payout.is_negative:
        raise InvalidAmount("A payout cannot be negative; use zero for a total loss.")

    with idempotent(idempotency_key, scope="wallet.claim_risked",
                    payload={"credit": str(credit.pk)}) as h:
        if h.replayed:
            return Amount(credit.settled_minor or 0, Asset(credit.asset))

        house = system_account(AccountKind.HOUSE_REVENUE, credit.amount.asset)
        legs = [
            # The stake leaves the user and joins the house bankroll...
            Leg(user_account(credit.user, AccountKind.USER_PENDING, credit.amount.asset),
                -credit.amount),
            Leg(house, credit.amount),
        ]
        if payout.is_positive:
            # ...and the payout comes back out of that same bankroll. Never
            # minted: a jackpot is the house's earlier winnings changing hands.
            legs += [
                Leg(house, -payout),
                Leg(user_account(credit.user, AccountKind.USER_AVAILABLE, payout.asset),
                    payout),
            ]

        post(
            kind=EntryKind.CLAIM_RISKED,
            legs=legs,
            description=f"Risked claim {credit.amount} → {payout}",
            reference_type="pending_credit",
            reference_id=str(credit.pk),
            idempotency_key=idempotency_key,
            allow_overdraft=True,  # the house bankroll may dip on a lucky round
        )

        credit.state = CreditState.CLAIMED if payout.is_positive else CreditState.BURNED
        credit.settled_minor = payout.minor
        credit.settled_at = timezone.now()
        credit.game_round_id = game_round_id
        credit.save(update_fields=["state", "settled_minor", "settled_at",
                                   "game_round_id", "updated_at"])

        margin = credit.amount - payout
        if margin.is_positive:
            recognise_revenue(amount=margin, source="claim_gamble", reference_id=credit.pk)

        bus.publish(
            CreditClaimed(
                credit_id=credit.id, user_id=credit.user_id, settled_minor=payout.minor,
                original_minor=credit.amount_minor, asset=credit.amount.asset, risked=True,
            )
        )
        h.result_ref = str(credit.pk)
        return payout


def _lock_claimable(credit: PendingCredit) -> PendingCredit:
    """Re-read the credit under a row lock and prove it is still claimable.

    Without the lock, a double-clicked claim button races itself and pays twice;
    the idempotency key catches identical retries, but two genuinely concurrent
    requests need the database to serialise them.
    """
    credit = PendingCredit.objects.select_for_update().get(pk=credit.pk)
    if credit.state != CreditState.PENDING:
        raise IllegalStateTransition(current=credit.state, attempted="claim")
    if credit.expires_at <= timezone.now():
        raise IllegalStateTransition(current="expired", attempted="claim")
    return credit


@transaction.atomic
def expire_credit(credit: PendingCredit) -> None:
    """Sweep an unclaimed credit into the redistribution pool.

    Not confiscation for its own sake: the pool is what pays other users' idle
    drips, so a forgotten prize becomes somebody else's pleasant surprise
    instead of an open-ended liability on our balance sheet.
    """
    credit = PendingCredit.objects.select_for_update().get(pk=credit.pk)
    if credit.state != CreditState.PENDING:
        return

    post(
        kind=EntryKind.CREDIT_EXPIRY,
        legs=[
            Leg(user_account(credit.user, AccountKind.USER_PENDING, credit.amount.asset),
                -credit.amount),
            Leg(system_account(AccountKind.REDISTRIBUTION_POOL, credit.amount.asset),
                credit.amount),
        ],
        description=f"Expired unclaimed {credit.amount}",
        reference_type="pending_credit",
        reference_id=str(credit.pk),
    )
    credit.state = CreditState.EXPIRED
    credit.settled_minor = 0
    credit.settled_at = timezone.now()
    credit.save(update_fields=["state", "settled_minor", "settled_at", "updated_at"])


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------


@transaction.atomic
def place_hold(*, user, amount: Amount, purpose: str, reference_type: str = "",
               reference_id: str = "", expires_in: timedelta | None = None) -> Hold:
    """Reserve funds so they cannot be spent twice while something is in flight."""
    if not amount.is_positive:
        raise InvalidAmount("A hold must be for a positive amount.")

    hold = Hold.objects.create(
        user=user, amount_minor=amount.minor, asset=amount.asset, purpose=purpose,
        reference_type=reference_type, reference_id=reference_id,
        expires_at=timezone.now() + expires_in if expires_in else None,
    )
    post(
        kind=EntryKind.HOLD,
        legs=[
            Leg(user_account(user, AccountKind.USER_AVAILABLE, amount.asset), -amount),
            Leg(user_account(user, AccountKind.USER_HELD, amount.asset), amount),
        ],
        description=f"Hold for {purpose}",
        reference_type="hold",
        reference_id=str(hold.pk),
    )
    bus.publish(
        HoldPlaced(hold_id=hold.id, user_id=user.id, amount_minor=amount.minor,
                   asset=amount.asset, purpose=purpose)
    )
    return hold


@transaction.atomic
def release_hold(hold: Hold) -> None:
    """Give the money back — the sale aborted, the bid was outbid, the duel expired."""
    hold = Hold.objects.select_for_update().get(pk=hold.pk)
    if hold.state != HoldState.ACTIVE:
        return

    post(
        kind=EntryKind.HOLD_RELEASE,
        legs=[
            Leg(user_account(hold.user, AccountKind.USER_HELD, hold.amount.asset),
                -hold.amount),
            Leg(user_account(hold.user, AccountKind.USER_AVAILABLE, hold.amount.asset),
                hold.amount),
        ],
        description=f"Release hold {hold.purpose}",
        reference_type="hold",
        reference_id=str(hold.pk),
    )
    hold.state = HoldState.RELEASED
    hold.resolved_at = timezone.now()
    hold.save(update_fields=["state", "resolved_at", "updated_at"])


@transaction.atomic
def capture_hold(hold: Hold, *, to: AccountKind = AccountKind.ESCROW,
                 kind: EntryKind = EntryKind.PURCHASE) -> None:
    """Consume the hold — the purchase completed, the ticket is bought."""
    hold = Hold.objects.select_for_update().get(pk=hold.pk)
    if hold.state != HoldState.ACTIVE:
        raise IllegalStateTransition(current=hold.state, attempted="capture")

    post(
        kind=kind,
        legs=[
            Leg(user_account(hold.user, AccountKind.USER_HELD, hold.amount.asset),
                -hold.amount),
            Leg(system_account(to, hold.amount.asset), hold.amount),
        ],
        description=f"Capture hold {hold.purpose}",
        reference_type="hold",
        reference_id=str(hold.pk),
    )
    hold.state = HoldState.CAPTURED
    hold.resolved_at = timezone.now()
    hold.save(update_fields=["state", "resolved_at", "updated_at"])


# ---------------------------------------------------------------------------
# Peer transfer
# ---------------------------------------------------------------------------


@transaction.atomic
def gift(*, sender, recipient, amount: Amount, idempotency_key: str) -> PendingCredit:
    """Send biglie to another user. They arrive as BIGLIA_DONO, pending a claim.

    The asset changes on the way across: whatever the sender spent, the
    recipient receives as DONO, which is not withdrawable. Without that,
    peer transfer would be a clean pipe for turning someone else's deposited
    euros into your own withdrawal, which is the shape of a laundering rail.
    """
    if sender.pk == recipient.pk:
        raise InvalidAmount("Cannot gift to yourself.")
    if not amount.asset.is_transferable:
        raise AssetNotTransferable(
            f"{amount.asset.label} cannot be gifted. Winnings stay with the winner."
        )
    if not amount.is_positive:
        raise InvalidAmount("A gift must be positive.")

    with idempotent(idempotency_key, scope="wallet.gift",
                    payload={"to": str(recipient.pk), "amt": amount.minor}) as h:
        if h.replayed:
            return PendingCredit.objects.get(pk=h.result_ref)

        available = user_account(sender, AccountKind.USER_AVAILABLE, amount.asset)
        if available.balance_minor < amount.minor:
            raise InsufficientFunds(
                required=amount, available=Amount(available.balance_minor, amount.asset)
            )

        received = Amount(amount.minor, Asset.BIGLIA_DONO)
        post(
            kind=EntryKind.GIFT,
            legs=[
                # Retire the sender's units...
                Leg(available, -amount),
                Leg(system_account(AccountKind.ISSUANCE, amount.asset), amount),
                # ...and issue the same quantity as DONO to the recipient.
                Leg(system_account(AccountKind.ISSUANCE, Asset.BIGLIA_DONO), -received),
                Leg(user_account(recipient, AccountKind.USER_PENDING, Asset.BIGLIA_DONO),
                    received),
            ],
            description=f"Gift {amount} from {sender.pk} to {recipient.pk}",
            reference_type="gift",
            reference_id=str(sender.pk),
            idempotency_key=idempotency_key,
        )

        credit = _record_pending(
            user=recipient, amount=received, source=CreditSource.GIFT,
            reference_type="gift", reference_id=str(sender.pk),
        )
        bus.publish(
            FundsTransferred(
                from_user_id=sender.id, to_user_id=recipient.id,
                amount_minor=amount.minor, asset=amount.asset, reason="gift",
            )
        )
        h.result_ref = str(credit.pk)
        return credit


# ---------------------------------------------------------------------------
# Revenue, charity, redistribution
# ---------------------------------------------------------------------------


@transaction.atomic
def recognise_revenue(*, amount: Amount, source: str, reference_id=None) -> None:
    """Book a house cut and immediately carve out charity and redistribution.

    Called by every context that takes a fee. The two carve-outs happen here,
    synchronously, in the same transaction — not on a nightly job — so the
    charity figure on the public page is always a real ledger balance rather
    than an estimate, and the drip pool can never promise more than was earned.
    """
    if not amount.is_positive:
        return

    house = system_account(AccountKind.HOUSE_REVENUE, amount.asset)
    charity_cut, _ = split_percentage(amount, CHARITY_BASIS_POINTS)
    redistribution_cut, _ = split_percentage(amount, REDISTRIBUTION_BASIS_POINTS)

    legs: list[Leg] = []
    if charity_cut:
        legs += [
            Leg(house, -charity_cut),
            Leg(system_account(AccountKind.CHARITY_POOL, amount.asset), charity_cut),
        ]
    if redistribution_cut:
        legs += [
            Leg(house, -redistribution_cut),
            Leg(system_account(AccountKind.REDISTRIBUTION_POOL, amount.asset),
                redistribution_cut),
        ]

    if legs:
        post(
            kind=EntryKind.CHARITY_ALLOCATION,
            legs=legs,
            description=f"Carve-out from {source}",
            reference_type=source,
            reference_id=str(reference_id or ""),
            allow_overdraft=True,
        )

    bus.publish(
        RevenueRecognised(
            amount_minor=amount.minor, asset=amount.asset, source=source,
            reference_id=reference_id,
        )
    )
    if charity_cut:
        bus.publish(
            CharityAllocated(
                amount_minor=charity_cut.minor, asset=amount.asset,
                basis_points=CHARITY_BASIS_POINTS, beneficiary_id=None,
            )
        )


@transaction.atomic
def drip(*, user, amount: Amount, idempotency_key: str) -> PendingCredit | None:
    """Pay a passive reward out of the redistribution pool.

    Returns None if the pool cannot cover it. That is the whole design: the
    idle-user reward is redistribution of money the platform actually earned,
    so it can never be the thing that inflates the currency. A quiet week means
    a thin drip, and the economy stays sound.
    """
    pool = system_account(AccountKind.REDISTRIBUTION_POOL, amount.asset)
    if pool.balance_minor < amount.minor:
        logger.info("Drip skipped: pool holds %s, needed %s", pool.balance_minor, amount.minor)
        return None

    with idempotent(idempotency_key, scope="wallet.drip",
                    payload={"user": str(user.pk), "amt": amount.minor}) as h:
        if h.replayed:
            return PendingCredit.objects.get(pk=h.result_ref)

        post(
            kind=EntryKind.DRIP,
            legs=[
                Leg(pool, -amount),
                Leg(user_account(user, AccountKind.USER_PENDING, amount.asset), amount),
            ],
            description=f"Passive drip {amount}",
            reference_type="drip",
            reference_id=str(user.pk),
            idempotency_key=idempotency_key,
        )
        credit = _record_pending(
            user=user, amount=amount, source=CreditSource.DRIP,
            reference_type="drip", reference_id=str(user.pk),
        )
        bus.publish(
            DripAwarded(user_id=user.id, amount_minor=amount.minor, asset=amount.asset)
        )
        h.result_ref = str(credit.pk)
        return credit

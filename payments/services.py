"""The payments context's public API.

Two flows live here and nothing else does:

* **Deposit** — the only inflationary path in the platform. A PaymentIntent is
  created, the user pays on the provider's surface, the provider tells us on a
  signed webhook, and only then does `wallet.services.issue_for_deposit` mint
  biglie. No view, task or admin action can shortcut that order.

* **Withdrawal** — the mirror. Only BIGLIA_PIENA may leave, the funds are held
  the moment the request is confirmed, compliance decides whether a human looks,
  and `wallet.services.burn_for_withdrawal` destroys the supply as the euros go
  out.

Everything that moves money takes an idempotency key derived from a stable id,
because both halves of this file are reachable from a retrying HTTP client.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from compliance.constants import LimitKind
from compliance.services import check_limit, record_usage, require_kyc_for_withdrawal
from core.contracts import EURO_PER_BIGLIA, DepositConfirmed, WithdrawalRequested
from core.events import bus
from core.exceptions import (
    AgeVerificationRequired,
    AssetNotWithdrawable,
    IllegalStateTransition,
    InsufficientFunds,
    InvalidAmount,
    SelfExcluded,
)
from core.money import Amount, Asset
from payments.constants import (
    AUTO_APPROVE_MAX_MINOR,
    MAX_DEPOSIT_MINOR,
    MAX_WITHDRAWAL_MINOR,
    MIN_DEPOSIT_MINOR,
    MIN_WITHDRAWAL_MINOR,
    WITHDRAWAL_HOLD_PURPOSE,
    PaymentState,
)
from payments.models import Deposit, Withdrawal
from payments.providers import get_provider
from wallet.services import (
    burn_for_withdrawal,
    issue_for_deposit,
    place_hold,
    release_hold,
    summary_for,
)

logger = logging.getLogger("payments")

#: The refusal at the centre of the whole anti-laundering design. Written out in
#: full because a user who is told "non puoi" without being told *why* assumes
#: the platform is stealing from them — and because the reason is a good one.
NON_WITHDRAWABLE_EXPLANATION = (
    "Solo le biglie piene — quelle comprate con denaro reale — possono tornare a "
    "essere denaro. Le biglie vinte e quelle ricevute in regalo si spendono e si "
    "giocano su biglie, ma non si prelevano: se si potesse, depositare, giocare "
    "una volta e riprelevare trasformerebbe il sito in un canale per spostare "
    "denaro altrui, ed è esattamente la forma che ha il riciclaggio."
)


# ---------------------------------------------------------------------------
# Exchange
# ---------------------------------------------------------------------------


def biglie_for_euro(euro: Amount) -> Amount:
    """How many biglie a euro amount buys. Parity today, a rate tomorrow.

    Integer maths through Decimal, rounding down: the platform never issues a
    scaglia it was not paid for.
    """
    if euro.asset is not Asset.EUR:
        raise InvalidAmount("Deposits are priced in euros.")
    minor = int((Decimal(euro.minor) / EURO_PER_BIGLIA).to_integral_value(ROUND_DOWN))
    return Amount(minor, Asset.BIGLIA_PIENA)


def euro_for_biglie(biglie: Amount) -> Amount:
    """What withdrawing these biglie pays out."""
    assert_withdrawable(biglie.asset)
    minor = int((Decimal(biglie.minor) * EURO_PER_BIGLIA).to_integral_value(ROUND_DOWN))
    return Amount(minor, Asset.EUR)


def assert_withdrawable(asset: Asset) -> None:
    """The line from docs/ECONOMY.md, enforced before a row is even created."""
    if asset is Asset.BIGLIA_PIENA:
        return
    error = AssetNotWithdrawable(NON_WITHDRAWABLE_EXPLANATION)
    # Shadow the generic class-level message: this refusal is the one place the
    # user deserves the whole reasoning rather than "non convertibili".
    error.user_message = NON_WITHDRAWABLE_EXPLANATION
    raise error


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------


def _assert_may_transact(user) -> None:
    """Age and account state. Checked on both directions of money movement."""
    if not user.is_of_age:
        raise AgeVerificationRequired()
    if not user.is_active:
        error = SelfExcluded("Account is not active.")
        error.user_message = "Questo conto non è attivo. Scrivi all'assistenza."
        raise error


@transaction.atomic
def start_deposit(*, user, euro: Amount, provider_key: str | None = None) -> Deposit:
    """Create a PaymentIntent and the row that tracks it.

    Nothing is credited here — this only opens the door. The deposit sits in
    PENDING until a signed webhook says the money actually arrived.
    """
    if euro.asset is not Asset.EUR:
        raise InvalidAmount("Deposits are priced in euros.")
    if not MIN_DEPOSIT_MINOR <= euro.minor <= MAX_DEPOSIT_MINOR:
        raise InvalidAmount(
            f"L'importo deve essere fra {Amount(MIN_DEPOSIT_MINOR, Asset.EUR)} e "
            f"{Amount(MAX_DEPOSIT_MINOR, Asset.EUR)}."
        )

    _assert_may_transact(user)
    if user.is_self_excluded:
        # Withdrawing stays open while excluded; putting money *in* does not.
        raise SelfExcluded()

    # The user's own deposit limit, checked before the intent exists so they are
    # never shown a payment sheet for money we would have to refuse.
    check_limit(user, LimitKind.DEPOSIT, euro)

    provider = get_provider(provider_key)
    deposit = Deposit.objects.create(
        user=user,
        euro_minor=euro.minor,
        biglie_minor=biglie_for_euro(euro).minor,
        provider=str(provider.key),
        state=PaymentState.INITIATED,
    )

    intent = provider.create_deposit_intent(
        amount=euro,
        reference=str(deposit.id),
        idempotency_key=f"deposit-intent:{deposit.id}",
        metadata={"user_id": str(user.pk)},
    )

    deposit.transition_to(
        PaymentState.PENDING,
        provider_reference=intent.reference,
        client_secret=intent.client_secret,
    )
    logger.info("deposit_started id=%s user=%s amount=%s", deposit.id, user.pk, euro.minor)
    return deposit


@transaction.atomic
def confirm_deposit(*, deposit: Deposit, paid_minor: int | None = None) -> Deposit:
    """Settle a deposit: mint the biglie, publish, record the limit usage.

    Idempotent twice over. The webhook layer refuses to run this a second time
    for the same provider event, and `issue_for_deposit` refuses to post a
    second journal entry for the same deposit id even if it somehow is called
    again — which matters, because "credited twice" is indistinguishable from
    "printed money".
    """
    deposit = Deposit.objects.select_for_update().get(pk=deposit.pk)

    if deposit.state == PaymentState.SUCCEEDED:
        logger.info("deposit_already_settled id=%s", deposit.id)
        return deposit
    if not deposit.can_transition_to(PaymentState.SUCCEEDED):
        raise IllegalStateTransition(current=str(deposit.state), attempted="succeeded")

    if paid_minor is not None and paid_minor != deposit.euro_minor:
        # Never quietly credit a different number than the one we asked for.
        logger.error(
            "deposit_amount_mismatch id=%s expected=%s paid=%s",
            deposit.id, deposit.euro_minor, paid_minor,
        )
        raise InvalidAmount(
            f"Deposit {deposit.id} expected {deposit.euro_minor} but the provider "
            f"settled {paid_minor}."
        )

    issue_for_deposit(
        user=deposit.user,
        euro=deposit.euro,
        biglie=deposit.biglie,
        idempotency_key=f"deposit:{deposit.id}",
        reference_id=str(deposit.id),
    )
    deposit.mark_confirmed()

    # Counts against the limit the user set for themselves. Recorded after the
    # money is real, so a failed card never eats someone's daily allowance.
    record_usage(
        deposit.user, LimitKind.DEPOSIT, deposit.euro, reference=f"deposit:{deposit.id}"
    )

    bus.publish(
        DepositConfirmed(
            deposit_id=deposit.id,
            user_id=deposit.user_id,
            euro_minor=deposit.euro_minor,
            biglie_minor=deposit.biglie_minor,
        )
    )
    logger.info("deposit_confirmed id=%s user=%s", deposit.id, deposit.user_id)
    return deposit


@transaction.atomic
def fail_deposit(*, deposit: Deposit, code: str = "", message: str = "") -> Deposit:
    """The card was declined or the intent was abandoned. No money moved."""
    deposit = Deposit.objects.select_for_update().get(pk=deposit.pk)
    if deposit.state == PaymentState.FAILED:
        return deposit
    deposit.mark_failed(code=code, message=message)
    logger.info("deposit_failed id=%s code=%s", deposit.id, code)
    return deposit


@transaction.atomic
def reverse_deposit(*, deposit: Deposit, reason: str) -> Deposit:
    """A settled deposit was charged back.

    Deliberately does *not* claw the biglie back automatically: by the time a
    dispute lands the user may have spent, gambled or gifted them, and a forced
    negative balance would corrupt the ledger's central invariant. The row is
    marked, the incident is logged at ERROR, and a human decides — which is the
    honest answer, because a chargeback is a fraud investigation and not an
    arithmetic operation.
    """
    deposit = Deposit.objects.select_for_update().get(pk=deposit.pk)
    if deposit.state == PaymentState.REVERSED:
        return deposit
    deposit.mark_reversed(reason=reason)
    logger.error(
        "deposit_reversed id=%s user=%s amount=%s reason=%s — manual clawback required",
        deposit.id, deposit.user_id, deposit.euro_minor, reason,
    )
    return deposit


# ---------------------------------------------------------------------------
# Withdrawals
# ---------------------------------------------------------------------------


def destination_fingerprint(iban: str) -> str:
    """A stable, non-reversible handle for a bank account.

    We keep this instead of the IBAN. It still answers the question an AML
    analyst actually asks — "have two accounts paid out to the same bank
    account?" — while a database leak yields nobody's account number.
    """
    normalised = "".join(iban.split()).upper()
    digest = hmac.new(
        settings.SECRET_KEY.encode(), normalised.encode(), hashlib.sha256
    ).hexdigest()
    return f"ban_{digest[:32]}"


def available_piena(user) -> Amount:
    """What this user could withdraw right now, straight from the wallet."""
    return summary_for(user)["balances"].get(
        Asset.BIGLIA_PIENA, Amount.zero(Asset.BIGLIA_PIENA)
    )


@transaction.atomic
def open_withdrawal(*, user, biglie: Amount, iban: str) -> Withdrawal:
    """Draft a withdrawal. Validates everything, moves nothing.

    Splitting the draft from the confirmation is not ceremony: it is what lets
    the confirm screen state the amount, the destination and the fact that
    winnings cannot be withdrawn *before* anything is reserved.
    """
    assert_withdrawable(biglie.asset)

    if not MIN_WITHDRAWAL_MINOR <= biglie.minor <= MAX_WITHDRAWAL_MINOR:
        raise InvalidAmount(
            f"Puoi prelevare da {Amount(MIN_WITHDRAWAL_MINOR, Asset.BIGLIA_PIENA)} a "
            f"{Amount(MAX_WITHDRAWAL_MINOR, Asset.BIGLIA_PIENA)} per volta."
        )

    _assert_may_transact(user)

    available = available_piena(user)
    if available < biglie:
        raise InsufficientFunds(
            required=biglie,
            available=available,
            message="Non hai abbastanza biglie piene per questo prelievo.",
        )

    euro = euro_for_biglie(biglie)

    # KYC is checked here as well as at confirmation so the user is told before
    # they type their IBAN, not after.
    require_kyc_for_withdrawal(user, euro)

    withdrawal = Withdrawal.objects.create(
        user=user,
        biglie_minor=biglie.minor,
        asset=biglie.asset,
        euro_minor=euro.minor,
        destination_reference=destination_fingerprint(iban),
        destination_last4="".join(iban.split())[-4:],
        state=PaymentState.INITIATED,
    )
    logger.info("withdrawal_opened id=%s user=%s amount=%s", withdrawal.id, user.pk, biglie.minor)
    return withdrawal


@transaction.atomic
def confirm_withdrawal(withdrawal: Withdrawal) -> Withdrawal:
    """The user said yes. Reserve the funds and hand it to review or to payout.

    The hold is what makes waiting for review safe: the money is out of the
    user's spendable balance from this moment, so a queued withdrawal can never
    be spent out from under itself.

    There is deliberately **no** "annulla il prelievo e continua a giocare"
    path here or anywhere else in this context. Offering to hand a pending
    withdrawal back as playable balance is the single most effective dark
    pattern in gambling products — it converts a decision to stop into another
    session — and we are not building it. A user who changes their mind can
    deposit again, which requires the same deliberate act as the first time.
    """
    withdrawal = Withdrawal.objects.select_for_update().get(pk=withdrawal.pk)
    if withdrawal.state != PaymentState.INITIATED:
        raise IllegalStateTransition(current=str(withdrawal.state), attempted="pending")

    require_kyc_for_withdrawal(withdrawal.user, withdrawal.euro)

    hold = place_hold(
        user=withdrawal.user,
        amount=withdrawal.biglie,
        purpose=WITHDRAWAL_HOLD_PURPOSE,
        reference_type="withdrawal",
        reference_id=str(withdrawal.id),
    )

    requires_review = _requires_manual_review(withdrawal)
    withdrawal.transition_to(
        PaymentState.PENDING,
        hold_id=hold.id,
        confirmed_at=timezone.now(),
        requires_review=requires_review,
    )

    # Published from here rather than from the draft: this is the moment the
    # money is actually committed, and compliance's audit journal listens.
    bus.publish(
        WithdrawalRequested(
            withdrawal_id=withdrawal.id,
            user_id=withdrawal.user_id,
            euro_minor=withdrawal.euro_minor,
        )
    )
    logger.info(
        "withdrawal_confirmed id=%s review=%s amount=%s",
        withdrawal.id, requires_review, withdrawal.euro_minor,
    )

    if not requires_review:
        return approve_withdrawal(withdrawal, automatic=True)
    return withdrawal


def _requires_manual_review(withdrawal: Withdrawal) -> bool:
    """Who gets paid without a human looking.

    Small, from a verified account, is automatic. Everything else queues. The
    threshold is a policy dial in `payments.constants`, not a magic number here.
    """
    if withdrawal.euro_minor > AUTO_APPROVE_MAX_MINOR:
        return True
    return not withdrawal.user.is_identity_verified


@transaction.atomic
def approve_withdrawal(
    withdrawal: Withdrawal, *, approved_by=None, automatic: bool = False
) -> Withdrawal:
    """Burn the biglie and send the euros.

    Order matters and is the opposite of what feels natural: the payout call
    happens *inside* the transaction that burns the supply, so a provider
    failure rolls the burn back rather than leaving a user with neither their
    biglie nor their money. The payout carries an idempotency key derived from
    the withdrawal id, so a retry after a network timeout cannot double-pay.
    """
    withdrawal = Withdrawal.objects.select_for_update().get(pk=withdrawal.pk)
    if withdrawal.state == PaymentState.SUCCEEDED:
        return withdrawal
    if withdrawal.state != PaymentState.PENDING:
        raise IllegalStateTransition(current=str(withdrawal.state), attempted="succeeded")

    # The hold reserved the funds; releasing it puts them back in the available
    # balance for the instant it takes to burn them. Both legs are in this
    # transaction, so there is no window where the user could spend them.
    if withdrawal.hold_id:
        release_hold(_hold_for(withdrawal))

    burn_for_withdrawal(
        user=withdrawal.user,
        biglie=withdrawal.biglie,
        euro=withdrawal.euro,
        idempotency_key=f"withdrawal:{withdrawal.id}",
        reference_id=str(withdrawal.id),
    )

    provider = get_provider()
    payout = provider.create_payout(
        amount=withdrawal.euro,
        destination=withdrawal.destination_reference,
        reference=str(withdrawal.id),
        idempotency_key=f"withdrawal-payout:{withdrawal.id}",
    )

    withdrawal.transition_to(
        PaymentState.SUCCEEDED,
        completed_at=timezone.now(),
        provider=str(provider.key),
        provider_reference=payout.reference,
        approved_automatically=automatic,
        reviewed_by=approved_by,
        reviewed_at=timezone.now(),
        requires_review=False,
    )

    # Feeds `compliance.services.cumulative_withdrawn_minor`, which is what
    # decides when BASIC verification stops being enough. The "withdrawal:"
    # prefix is the marker compliance looks for.
    record_usage(
        withdrawal.user,
        LimitKind.DEPOSIT,
        withdrawal.euro,
        reference=f"withdrawal:{withdrawal.id}",
    )

    logger.info(
        "withdrawal_paid id=%s user=%s amount=%s automatic=%s",
        withdrawal.id, withdrawal.user_id, withdrawal.euro_minor, automatic,
    )
    return withdrawal


@transaction.atomic
def reject_withdrawal(withdrawal: Withdrawal, *, reason: str, rejected_by=None) -> Withdrawal:
    """A reviewer said no. The hold is released and the biglie stay the user's.

    Note what this is not: it is not a cancel button offered to the *user*. The
    only people who can stop a confirmed withdrawal are the ones who have a
    compliance reason to.
    """
    withdrawal = Withdrawal.objects.select_for_update().get(pk=withdrawal.pk)
    if withdrawal.state != PaymentState.PENDING:
        raise IllegalStateTransition(current=str(withdrawal.state), attempted="failed")

    if withdrawal.hold_id:
        release_hold(_hold_for(withdrawal))

    withdrawal.transition_to(
        PaymentState.FAILED,
        failed_at=timezone.now(),
        rejection_reason=reason[:255],
        reviewed_by=rejected_by,
        reviewed_at=timezone.now(),
        requires_review=False,
    )
    logger.info("withdrawal_rejected id=%s reason=%s", withdrawal.id, reason)
    return withdrawal


@transaction.atomic
def reverse_withdrawal(withdrawal: Withdrawal, *, reason: str) -> Withdrawal:
    """The payout bounced and the euros came back to us.

    Re-issuing through `issue_for_deposit` looks surprising for a moment and is
    exactly right: real euros are sitting in our account and the matching biglie
    were destroyed, which is the definition of a deposit. Doing it any other way
    would mean minting outside the one mint.
    """
    withdrawal = Withdrawal.objects.select_for_update().get(pk=withdrawal.pk)
    if withdrawal.state != PaymentState.SUCCEEDED:
        raise IllegalStateTransition(current=str(withdrawal.state), attempted="reversed")

    issue_for_deposit(
        user=withdrawal.user,
        euro=withdrawal.euro,
        biglie=withdrawal.biglie,
        idempotency_key=f"withdrawal-reversal:{withdrawal.id}",
        reference_id=str(withdrawal.id),
    )
    withdrawal.transition_to(
        PaymentState.REVERSED,
        reversed_at=timezone.now(),
        rejection_reason=reason[:255],
    )
    logger.error(
        "withdrawal_reversed id=%s user=%s reason=%s", withdrawal.id, withdrawal.user_id, reason
    )
    return withdrawal


def _hold_for(withdrawal: Withdrawal):
    """Re-read the wallet hold this withdrawal reserved.

    A gap in the wallet's published API: `release_hold(hold)` takes an object,
    but neither `wallet.services` nor `wallet.selectors` offers a way to look a
    hold up by id, and a withdrawal awaiting review is released by a different
    request than the one that placed it. Until `wallet.selectors.get_hold(id)`
    (or a `release_hold_by_id`) exists, we go through the name the wallet's own
    service module publishes. Kept in one function so there is exactly one line
    to change when it does.
    """
    from wallet.services import Hold

    return Hold.objects.get(pk=withdrawal.hold_id)


# ---------------------------------------------------------------------------
# Read helpers for the views
# ---------------------------------------------------------------------------


def recent_movements(user, *, limit: int = 20) -> dict:
    """Both sides of a user's real-money history, newest first."""
    return {
        "deposits": list(Deposit.objects.filter(user=user).order_by("-created_at")[:limit]),
        "withdrawals": list(
            Withdrawal.objects.filter(user=user).order_by("-created_at")[:limit]
        ),
    }

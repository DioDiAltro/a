"""The provider callback — the most security-sensitive endpoint in the platform.

Two properties, and the deposit flow is only safe if both hold:

1. **Authenticated.** The payload is rejected unless it carries a valid HMAC
   signature over the exact bytes we received, within a timestamp tolerance. An
   unverified webhook endpoint that credits wallets is an open mint: anyone who
   learns the URL can POST themselves money.

2. **Idempotent.** Stripe retries until it gets a 2xx, and it also redelivers
   after network hiccups. The same `evt_...` id must therefore be worth exactly
   one credit — enforced with `core.idempotency.idempotent` keyed on the event
   id, inside the same transaction that does the work, so a rollback also
   forgets that the event was handled.

The endpoint returns 400 only for a failed signature (Stripe stops retrying a
400), 500 when we could not process something we should have (Stripe retries),
and 200 for everything else including events we do not care about — a non-2xx
for an event we simply ignore would have Stripe retrying it for days.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseServerError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.exceptions import BiglieError
from core.idempotency import idempotent
from payments.constants import HANDLED_EVENT_TYPES, PaymentState
from payments.models import Deposit, ProviderEventLog, Withdrawal
from payments.providers import ProviderEvent, WebhookSignatureError, get_provider
from payments.services import (
    confirm_deposit,
    fail_deposit,
    reverse_deposit,
    reverse_withdrawal,
)

logger = logging.getLogger("payments.webhook")

#: Scope for the idempotency marker. The key itself is the provider event id.
IDEMPOTENCY_SCOPE = "payments.provider_event"


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Entry point. Verifies, then hands off to the idempotent handler."""
    provider = get_provider()

    try:
        event = provider.verify_webhook(
            payload=request.body,
            signature=request.headers.get("Stripe-Signature", ""),
        )
    except WebhookSignatureError as exc:
        # Deliberately terse to the caller and loud in the log: a failing
        # signature is either a misconfiguration or somebody probing for a way
        # to credit a wallet, and both deserve a look.
        logger.warning("webhook_signature_rejected: %s", exc)
        return HttpResponseBadRequest("signature verification failed")

    try:
        outcome = handle_event(event, provider_key=str(provider.key))
    except BiglieError as exc:
        # A domain refusal (amount mismatch, illegal transition). Retrying will
        # not help, so acknowledge and let the ERROR log carry it to a human.
        logger.error("webhook_refused id=%s type=%s: %s", event.id, event.type, exc)
        return HttpResponse("acknowledged", status=200)
    except Exception:
        # Anything unexpected: ask the provider to send it again. Losing a
        # `payment_intent.succeeded` means a user paid and was never credited.
        logger.exception("webhook_failed id=%s type=%s", event.id, event.type)
        return HttpResponseServerError("retry please")

    return HttpResponse(outcome, status=200)


@transaction.atomic
def handle_event(event: ProviderEvent, *, provider_key: str) -> str:
    """Process one provider event at most once. Returns a one-line outcome."""
    with idempotent(
        event.id, scope=IDEMPOTENCY_SCOPE, payload={"type": event.type}
    ) as handle:
        if handle.replayed:
            # The redelivery path. This branch is the entire reason a Stripe
            # replay cannot credit a deposit twice.
            logger.info("webhook_replay_ignored id=%s type=%s", event.id, event.type)
            return "replayed"

        log, _ = ProviderEventLog.objects.get_or_create(
            provider=provider_key,
            event_id=event.id,
            defaults={"event_type": event.type, "payload": event.payload},
        )

        outcome = _dispatch(event, log)

        log.handled = True
        log.outcome = outcome[:255]
        log.save(update_fields=["handled", "outcome", "deposit", "withdrawal", "updated_at"])

        handle.result_ref = str(log.pk)
        return outcome


def _dispatch(event: ProviderEvent, log: ProviderEventLog) -> str:
    if event.type not in HANDLED_EVENT_TYPES:
        logger.info("webhook_ignored id=%s type=%s", event.id, event.type)
        return "ignored"

    if event.type.startswith("payout."):
        return _handle_payout(event, log)

    deposit = _find_deposit(event)
    if deposit is None:
        # Kept, not dropped: the signed payload is in ProviderEventLog and a
        # human can work out which account it belonged to.
        logger.error("webhook_orphan id=%s type=%s ref=%s", event.id, event.type,
                     event.intent_reference)
        return "no matching deposit"

    log.deposit = deposit

    if event.type == "payment_intent.succeeded":
        confirm_deposit(deposit=deposit, paid_minor=event.amount_minor)
        return "deposit confirmed"

    if event.type in ("payment_intent.payment_failed", "payment_intent.canceled"):
        if deposit.state in (PaymentState.SUCCEEDED, PaymentState.REVERSED):
            # A late failure for something already settled is not a reason to
            # unwind a credit; that path is a chargeback, handled below.
            return "ignored: deposit already settled"
        fail_deposit(
            deposit=deposit, code=event.failure_code, message=event.failure_message
        )
        return "deposit failed"

    if event.type in ("charge.refunded", "charge.dispute.created"):
        if deposit.state != PaymentState.SUCCEEDED:
            return "ignored: deposit not settled"
        reverse_deposit(deposit=deposit, reason=event.type)
        return "deposit reversed — manual clawback required"

    return "ignored"


def _find_deposit(event: ProviderEvent) -> Deposit | None:
    """Match the event to a deposit by PaymentIntent, then by our own metadata.

    Two routes because charge-level events carry the intent id while some
    provider objects only carry back the metadata we set at creation time.
    """
    reference = event.intent_reference
    if reference:
        deposit = (
            Deposit.objects.select_for_update()
            .filter(provider_reference=reference)
            .first()
        )
        if deposit is not None:
            return deposit

    deposit_id = event.metadata.get("deposit_id")
    if deposit_id:
        return Deposit.objects.select_for_update().filter(pk=deposit_id).first()

    return None


def _handle_payout(event: ProviderEvent, log: ProviderEventLog) -> str:
    """Payout lifecycle. A bounced payout is a reversal, not a failure.

    By the time `payout.failed` arrives the biglie are already burned, so the
    only correct response is to put the value back — which
    `payments.services.reverse_withdrawal` does through the one mint.
    """
    withdrawal = (
        Withdrawal.objects.select_for_update()
        .filter(provider_reference=event.payout_reference)
        .first()
    )
    if withdrawal is None:
        logger.error("webhook_orphan_payout id=%s ref=%s", event.id, event.payout_reference)
        return "no matching withdrawal"

    log.withdrawal = withdrawal

    if event.type == "payout.paid":
        return "payout confirmed"

    if withdrawal.state != PaymentState.SUCCEEDED:
        return "ignored: withdrawal not settled"

    reverse_withdrawal(withdrawal, reason="payout failed at the provider")
    return "withdrawal reversed"

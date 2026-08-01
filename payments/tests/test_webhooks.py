"""The webhook endpoint: the one URL where a bug prints money.

Two properties are load-bearing and each has a test that fails loudly if it
regresses: the payload must be authenticated, and the same provider event must
be worth exactly one credit no matter how many times it arrives.
"""

from __future__ import annotations

import json

import pytest
from django.urls import reverse

from core.money import Asset
from payments.constants import PaymentState
from payments.models import Deposit, ProviderEventLog
from payments.providers import signature_header
from payments.services import start_deposit
from payments.tests.conftest import biglie, euro
from wallet.constants import EntryKind
from wallet.ledger import circulating_supply
from wallet.models import JournalEntry, PendingCredit

PIENA = Asset.BIGLIA_PIENA


def post_webhook(client, payload: bytes, signature: str):
    return client.post(
        reverse("payments:stripe_webhook"),
        data=payload,
        content_type="application/json",
        headers={"stripe-signature": signature},
    )


@pytest.fixture
def pending_deposit(alice, provider):
    """A €50 deposit sitting in PENDING, waiting for the provider to confirm."""
    return start_deposit(user=alice, euro=euro("50"))


# ---------------------------------------------------------------------------
# Idempotency — the money-printing bug
# ---------------------------------------------------------------------------


def test_replayed_event_credits_exactly_once(client, provider, alice, pending_deposit):
    """Stripe redelivers. The second delivery must be worth nothing.

    Asserted at three levels because they can fail independently: one deposit
    row, one journal entry, one pending credit, and a circulating supply equal
    to a single deposit.
    """
    payload, signature = provider.paid_event(
        pending_deposit.provider_reference, event_id="evt_replay"
    )

    first = post_webhook(client, payload, signature)
    second = post_webhook(client, payload, signature)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.content.decode() == "replayed"

    pending_deposit.refresh_from_db()
    assert pending_deposit.state == PaymentState.SUCCEEDED

    assert Deposit.objects.count() == 1
    assert JournalEntry.objects.filter(kind=EntryKind.DEPOSIT).count() == 1
    assert PendingCredit.objects.filter(user=alice).count() == 1
    assert circulating_supply(PIENA) == biglie("50")


def test_replay_is_recorded_once_in_the_event_log(client, provider, pending_deposit):
    payload, signature = provider.paid_event(
        pending_deposit.provider_reference, event_id="evt_log_once"
    )
    post_webhook(client, payload, signature)
    post_webhook(client, payload, signature)

    assert ProviderEventLog.objects.filter(event_id="evt_log_once").count() == 1


def test_two_distinct_events_credit_twice(client, provider, alice):
    """The mirror of the replay test: idempotency must not swallow real events."""
    first_deposit = start_deposit(user=alice, euro=euro("10"))
    second_deposit = start_deposit(user=alice, euro=euro("15"))

    post_webhook(client, *provider.paid_event(first_deposit.provider_reference))
    post_webhook(client, *provider.paid_event(second_deposit.provider_reference))

    assert circulating_supply(PIENA) == biglie("25")
    assert PendingCredit.objects.filter(user=alice).count() == 2


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_unsigned_payload_is_refused(client, provider, pending_deposit):
    payload, _ = provider.paid_event(pending_deposit.provider_reference)

    response = post_webhook(client, payload, "")

    assert response.status_code == 400
    pending_deposit.refresh_from_db()
    assert pending_deposit.state == PaymentState.PENDING
    assert circulating_supply(PIENA).minor == 0


def test_payload_signed_with_the_wrong_secret_is_refused(client, provider, pending_deposit):
    payload, _ = provider.paid_event(pending_deposit.provider_reference)
    forged = signature_header(payload, "whsec_attacker")

    response = post_webhook(client, payload, forged)

    assert response.status_code == 400
    assert circulating_supply(PIENA).minor == 0


def test_tampered_payload_is_refused(client, provider, pending_deposit):
    """The amount is changed after signing — the signature must stop it."""
    payload, signature = provider.paid_event(pending_deposit.provider_reference)
    tampered = json.loads(payload)
    tampered["data"]["object"]["amount"] = 999_999

    response = post_webhook(
        client, json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode(), signature
    )

    assert response.status_code == 400
    assert circulating_supply(PIENA).minor == 0


def test_stale_signature_is_refused(client, provider, pending_deposit):
    """A signature captured yesterday cannot be replayed today."""
    payload, _ = provider.paid_event(pending_deposit.provider_reference)
    old = signature_header(payload, provider.webhook_secret, timestamp=1_600_000_000)

    assert post_webhook(client, payload, old).status_code == 400
    assert circulating_supply(PIENA).minor == 0


def test_get_is_not_allowed(client):
    assert client.get(reverse("payments:stripe_webhook")).status_code == 405


# ---------------------------------------------------------------------------
# The rest of the event vocabulary
# ---------------------------------------------------------------------------


def test_unknown_event_type_is_acknowledged_and_ignored(db, client, provider):
    """A 4xx for an event we do not care about means Stripe retries it forever."""
    payload, signature = provider.build_event("customer.created", {"id": "cus_123"})

    response = post_webhook(client, payload, signature)

    assert response.status_code == 200
    assert response.content.decode() == "ignored"


def test_failed_payment_marks_the_deposit_and_mints_nothing(
    client, provider, pending_deposit
):
    payload, signature = provider.failed_event(pending_deposit.provider_reference)

    assert post_webhook(client, payload, signature).status_code == 200

    pending_deposit.refresh_from_db()
    assert pending_deposit.state == PaymentState.FAILED
    assert pending_deposit.failure_code == "card_declined"
    assert circulating_supply(PIENA).minor == 0


def test_a_settled_deposit_ignores_a_late_failure(client, provider, pending_deposit):
    post_webhook(client, *provider.paid_event(pending_deposit.provider_reference))
    response = post_webhook(client, *provider.failed_event(pending_deposit.provider_reference))

    assert response.status_code == 200
    pending_deposit.refresh_from_db()
    assert pending_deposit.state == PaymentState.SUCCEEDED


def test_amount_mismatch_never_credits(client, provider, pending_deposit):
    """The provider reports a different figure than the one we asked for.

    Crediting "whatever arrived" is how an under-payment becomes free biglie.
    The deposit stays PENDING and a human gets an ERROR line.
    """
    intent = dict(provider.intents[pending_deposit.provider_reference])
    intent["status"] = "succeeded"
    intent["amount_received"] = intent["amount"] + 1_000
    intent.pop("idempotency_key", None)
    payload, signature = provider.build_event("payment_intent.succeeded", intent)

    response = post_webhook(client, payload, signature)

    assert response.status_code == 200  # acknowledged; retrying would not help
    pending_deposit.refresh_from_db()
    assert pending_deposit.state == PaymentState.PENDING
    assert circulating_supply(PIENA).minor == 0


def test_event_for_an_unknown_deposit_is_kept_not_dropped(db, client, provider):
    payload, signature = provider.build_event(
        "payment_intent.succeeded",
        {"id": "pi_never_seen", "amount": 1_000, "amount_received": 1_000, "metadata": {}},
    )

    response = post_webhook(client, payload, signature)

    assert response.status_code == 200
    assert response.content.decode() == "no matching deposit"
    # The signed payload survives for whoever has to work out what happened.
    assert ProviderEventLog.objects.filter(event_type="payment_intent.succeeded").count() == 1


def test_chargeback_marks_the_deposit_reversed_without_touching_the_ledger(
    client, provider, pending_deposit
):
    """A dispute is a fraud investigation, not an arithmetic operation.

    The biglie may already be spent; forcing a negative balance would corrupt
    the ledger's central invariant, so the row is flagged and a human decides.
    """
    post_webhook(client, *provider.paid_event(pending_deposit.provider_reference))

    payload, signature = provider.build_event(
        "charge.dispute.created",
        {"id": "dp_1", "payment_intent": pending_deposit.provider_reference},
    )
    assert post_webhook(client, payload, signature).status_code == 200

    pending_deposit.refresh_from_db()
    assert pending_deposit.state == PaymentState.REVERSED
    assert circulating_supply(PIENA) == biglie("50")


# ---------------------------------------------------------------------------
# What the rest of the platform hears
# ---------------------------------------------------------------------------


def test_confirmation_feeds_the_compliance_audit_journal(
    client, provider, pending_deposit, django_capture_on_commit_callbacks
):
    """`DepositConfirmed` is published, so AML logging happens for free."""
    from compliance.models import AuditEvent

    payload, signature = provider.paid_event(pending_deposit.provider_reference)
    with django_capture_on_commit_callbacks(execute=True):
        post_webhook(client, payload, signature)

    assert AuditEvent.objects.filter(event_name="payments.deposit_confirmed").count() == 1

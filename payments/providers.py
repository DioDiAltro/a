"""The payment provider seam.

Stripe is *an* implementation, not the design. Everything the rest of `payments`
knows about the outside world is in the three dataclasses and the four methods
below, which is what makes it possible to (a) run the entire deposit and
withdrawal flow in tests and in dev without a network call and (b) replace the
acquirer without touching a single line of domain logic.

The fake is not a mock: it signs its webhooks with the same HMAC scheme Stripe
uses, so the signature-verification path is exercised for real by the test suite
rather than stubbed out — which is the one path where "we tested the happy case"
is worth nothing.
"""

from __future__ import annotations

import abc
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from core.exceptions import BiglieError
from core.money import Amount, Asset
from payments.constants import ProviderKey

logger = logging.getLogger("payments.provider")

#: Stripe rejects webhooks whose timestamp is older than this. So do we: without
#: it, a signature captured once could be replayed forever.
SIGNATURE_TOLERANCE_SECONDS = 300


class ProviderError(BiglieError):
    """The acquirer refused or failed. Never leaks provider detail to the user."""

    user_message = "Il pagamento non è andato a buon fine. Riprova o usa un altro metodo."


class WebhookSignatureError(ProviderError):
    """The payload did not come from the provider — or was tampered with.

    Treated as hostile, not as an error: the request is dropped with a 400 and
    nothing is written. A webhook endpoint that credits wallets without checking
    this is an open mint.
    """

    user_message = "Richiesta non autenticata."


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderIntent:
    """A payment the user still has to complete on the provider's surface."""

    reference: str
    client_secret: str
    status: str = "requires_payment_method"


@dataclass(frozen=True, slots=True)
class ProviderPayout:
    """Money we asked the provider to send out."""

    reference: str
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """A verified, provider-agnostic webhook.

    `id` is the idempotency key for the whole system: providers redeliver, and
    the second delivery of a `payment_intent.succeeded` must be worth exactly
    zero biglie.
    """

    id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def object(self) -> dict[str, Any]:
        return self.payload.get("data", {}).get("object", {})

    @property
    def metadata(self) -> dict[str, Any]:
        return self.object.get("metadata") or {}

    @property
    def intent_reference(self) -> str:
        """The PaymentIntent this event is about, whatever object carries it."""
        obj = self.object
        if self.type.startswith("payment_intent."):
            return str(obj.get("id") or "")
        return str(obj.get("payment_intent") or "")

    @property
    def payout_reference(self) -> str:
        return str(self.object.get("id") or "") if self.type.startswith("payout.") else ""

    @property
    def amount_minor(self) -> int | None:
        for key in ("amount_received", "amount", "amount_refunded"):
            if key in self.object:
                return int(self.object[key])
        return None

    @property
    def failure_code(self) -> str:
        error = self.object.get("last_payment_error") or {}
        return str(error.get("code") or self.object.get("failure_code") or "")

    @property
    def failure_message(self) -> str:
        error = self.object.get("last_payment_error") or {}
        return str(error.get("message") or self.object.get("failure_message") or "")


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


class PaymentProvider(abc.ABC):
    """What `payments.services` is allowed to assume about the outside world."""

    key: ProviderKey

    @abc.abstractmethod
    def create_deposit_intent(
        self, *, amount: Amount, reference: str, idempotency_key: str,
        metadata: dict[str, str] | None = None,
    ) -> ProviderIntent:
        """Ask for money. `reference` is our deposit id, echoed back on the webhook."""

    @abc.abstractmethod
    def verify_webhook(self, *, payload: bytes, signature: str) -> ProviderEvent:
        """Authenticate a callback. Raises `WebhookSignatureError` if it is not ours."""

    @abc.abstractmethod
    def create_payout(
        self, *, amount: Amount, destination: str, reference: str, idempotency_key: str,
    ) -> ProviderPayout:
        """Send money out. Must be idempotent on `idempotency_key`."""

    # -- shared helpers -----------------------------------------------------

    @staticmethod
    def _check_euro(amount: Amount) -> None:
        if amount.asset is not Asset.EUR:
            raise ProviderError(
                f"A payment provider moves euros, not {amount.asset.label}."
            )
        if not amount.is_positive:
            raise ProviderError("A provider movement must be for a positive amount.")


# ---------------------------------------------------------------------------
# Signature scheme (shared by Stripe and the fake)
# ---------------------------------------------------------------------------


def signature_header(payload: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """Build a Stripe-format `Stripe-Signature` header for `payload`.

    Exposed because tests and the dev "simulate payment" button need to produce
    genuinely signed webhooks; nothing in production calls it.
    """
    timestamp = int(time.time()) if timestamp is None else timestamp
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _verify_signature(payload: bytes, header: str, secret: str) -> None:
    if not secret:
        raise WebhookSignatureError(
            "No webhook signing secret configured; refusing to trust the payload."
        )
    if not header:
        raise WebhookSignatureError("Missing signature header.")

    parts = dict(
        piece.split("=", 1) for piece in header.split(",") if "=" in piece
    )
    timestamp, provided = parts.get("t", ""), parts.get("v1", "")
    if not timestamp.isdigit() or not provided:
        raise WebhookSignatureError("Malformed signature header.")

    if abs(int(time.time()) - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
        raise WebhookSignatureError("Signature timestamp outside tolerance (replay?).")

    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    # Constant time: a timing oracle here would let an attacker forge a credit.
    if not hmac.compare_digest(expected, provided):
        raise WebhookSignatureError("Signature does not match the payload.")


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------


class StripeProvider(PaymentProvider):
    """The real acquirer. `stripe` is imported lazily so dev and CI never need it."""

    key = ProviderKey.STRIPE

    def __init__(self, *, secret_key: str = "", webhook_secret: str = ""):
        self.secret_key = secret_key or getattr(settings, "STRIPE_SECRET_KEY", "")
        self.webhook_secret = webhook_secret or getattr(settings, "STRIPE_WEBHOOK_SECRET", "")

    def _client(self):
        import stripe  # local import: optional dependency at runtime

        stripe.api_key = self.secret_key
        return stripe

    def create_deposit_intent(
        self, *, amount: Amount, reference: str, idempotency_key: str,
        metadata: dict[str, str] | None = None,
    ) -> ProviderIntent:
        self._check_euro(amount)
        stripe = self._client()
        try:
            intent = stripe.PaymentIntent.create(
                amount=amount.minor,
                currency="eur",
                automatic_payment_methods={"enabled": True},
                metadata={"deposit_id": reference, **(metadata or {})},
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 — provider taxonomy is not ours
            logger.exception("Stripe refused a PaymentIntent for deposit %s", reference)
            raise ProviderError(str(exc)) from exc

        return ProviderIntent(
            reference=intent["id"],
            client_secret=intent["client_secret"],
            status=intent["status"],
        )

    def verify_webhook(self, *, payload: bytes, signature: str) -> ProviderEvent:
        import stripe

        try:
            event = stripe.Webhook.construct_event(
                payload, signature, self.webhook_secret
            )
        except Exception as exc:  # signature or parse failure — both are hostile
            raise WebhookSignatureError(str(exc)) from exc

        raw = dict(event)
        return ProviderEvent(id=str(raw["id"]), type=str(raw["type"]), payload=raw)

    def create_payout(
        self, *, amount: Amount, destination: str, reference: str, idempotency_key: str,
    ) -> ProviderPayout:
        self._check_euro(amount)
        stripe = self._client()
        try:
            payout = stripe.Transfer.create(
                amount=amount.minor,
                currency="eur",
                destination=destination,
                metadata={"withdrawal_id": reference},
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Stripe refused a payout for withdrawal %s", reference)
            raise ProviderError(str(exc)) from exc

        return ProviderPayout(reference=payout["id"], status="pending")


# ---------------------------------------------------------------------------
# Fake
# ---------------------------------------------------------------------------


class FakeProvider(PaymentProvider):
    """Deterministic, offline, and signs for real.

    Keeps its intents in memory so a dev can walk the whole flow — create,
    "pay", webhook, credit — with no Stripe account and no tunnel.
    """

    key = ProviderKey.FAKE

    def __init__(self, *, webhook_secret: str = ""):
        self.webhook_secret = (
            webhook_secret or getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or "whsec_fake"
        )
        self.intents: dict[str, dict[str, Any]] = {}
        self.payouts: dict[str, dict[str, Any]] = {}

    def create_deposit_intent(
        self, *, amount: Amount, reference: str, idempotency_key: str,
        metadata: dict[str, str] | None = None,
    ) -> ProviderIntent:
        self._check_euro(amount)
        existing = next(
            (i for i in self.intents.values() if i["idempotency_key"] == idempotency_key),
            None,
        )
        if existing is not None:
            return ProviderIntent(
                reference=existing["id"],
                client_secret=existing["client_secret"],
                status=existing["status"],
            )

        pi_id = f"pi_fake_{uuid.uuid4().hex[:24]}"
        record = {
            "id": pi_id,
            "object": "payment_intent",
            "amount": amount.minor,
            "amount_received": 0,
            "currency": "eur",
            "status": "requires_payment_method",
            "client_secret": f"{pi_id}_secret_{uuid.uuid4().hex[:16]}",
            "metadata": {"deposit_id": reference, **(metadata or {})},
            "idempotency_key": idempotency_key,
        }
        self.intents[pi_id] = record
        return ProviderIntent(
            reference=pi_id, client_secret=record["client_secret"], status=record["status"]
        )

    def verify_webhook(self, *, payload: bytes, signature: str) -> ProviderEvent:
        _verify_signature(payload, signature, self.webhook_secret)
        try:
            raw = json.loads(payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookSignatureError("Payload is not JSON.") from exc
        if not isinstance(raw, dict) or "id" not in raw or "type" not in raw:
            raise WebhookSignatureError("Payload is not an event envelope.")
        return ProviderEvent(id=str(raw["id"]), type=str(raw["type"]), payload=raw)

    def create_payout(
        self, *, amount: Amount, destination: str, reference: str, idempotency_key: str,
    ) -> ProviderPayout:
        self._check_euro(amount)
        for payout in self.payouts.values():
            if payout["idempotency_key"] == idempotency_key:
                return ProviderPayout(reference=payout["id"], status=payout["status"])

        po_id = f"po_fake_{uuid.uuid4().hex[:24]}"
        self.payouts[po_id] = {
            "id": po_id,
            "amount": amount.minor,
            "destination": destination,
            "status": "pending",
            "idempotency_key": idempotency_key,
            "metadata": {"withdrawal_id": reference},
        }
        return ProviderPayout(reference=po_id, status="pending")

    # -- test / dev affordances --------------------------------------------

    def build_event(
        self, event_type: str, obj: dict[str, Any], *, event_id: str = "",
        timestamp: int | None = None,
    ) -> tuple[bytes, str]:
        """Return `(payload, signature_header)` for a genuinely signed event."""
        envelope = {
            "id": event_id or f"evt_fake_{uuid.uuid4().hex[:24]}",
            "type": event_type,
            "created": int(time.time()),
            "data": {"object": obj},
        }
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        return payload, signature_header(payload, self.webhook_secret, timestamp=timestamp)

    def paid_event(self, intent_reference: str, *, event_id: str = "") -> tuple[bytes, str]:
        """The webhook Stripe would send once the user actually pays."""
        intent = dict(self.intents[intent_reference])
        intent["status"] = "succeeded"
        intent["amount_received"] = intent["amount"]
        intent.pop("idempotency_key", None)
        self.intents[intent_reference]["status"] = "succeeded"
        return self.build_event("payment_intent.succeeded", intent, event_id=event_id)

    def failed_event(
        self, intent_reference: str, *, code: str = "card_declined", event_id: str = "",
    ) -> tuple[bytes, str]:
        intent = dict(self.intents[intent_reference])
        intent["status"] = "requires_payment_method"
        intent["last_payment_error"] = {"code": code, "message": "La carta è stata rifiutata."}
        intent.pop("idempotency_key", None)
        return self.build_event("payment_intent.payment_failed", intent, event_id=event_id)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_fake_singleton: FakeProvider | None = None


def get_provider(key: str | None = None) -> PaymentProvider:
    """The provider this deployment uses.

    The fake is refused outright when LIVE_MODE is on: a live deployment that
    "settles" payments against an in-memory dict would be minting biglie for
    free, and that must be impossible to reach by configuration accident.
    """
    global _fake_singleton

    chosen = key or getattr(settings, "PAYMENT_PROVIDER", "") or (
        ProviderKey.STRIPE if getattr(settings, "STRIPE_SECRET_KEY", "") else ProviderKey.FAKE
    )

    if chosen == ProviderKey.STRIPE:
        return StripeProvider()

    if getattr(settings, "LIVE_MODE", False):
        raise ImproperlyConfigured(
            "LIVE_MODE is on but the payment provider is the fake one. Set "
            "STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET, or turn LIVE_MODE off."
        )

    # One instance per process so an intent created by a view is visible to the
    # webhook that follows it.
    if _fake_singleton is None or _fake_singleton.webhook_secret != (
        getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or "whsec_fake"
    ):
        _fake_singleton = FakeProvider()
    return _fake_singleton


def reset_fake_provider() -> None:
    """Drop the in-process fake. Tests only."""
    global _fake_singleton
    _fake_singleton = None

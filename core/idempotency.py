"""Idempotent execution of money-moving operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from django.db import IntegrityError, transaction

from core.exceptions import IdempotencyConflict
from core.models import IdempotencyKey


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@contextmanager
def idempotent(key: str, *, scope: str, payload: dict[str, Any] | None = None):
    """Run a block at most once for `key`.

    Yields a small handle. If the operation already ran, ``handle.replayed`` is
    True and ``handle.result_ref`` holds whatever the first run recorded — the
    caller should return that instead of doing the work again.

    Usage::

        with idempotent(key, scope="wallet.claim", payload={"credit": str(pk)}) as h:
            if h.replayed:
                return Claim.objects.get(pk=h.result_ref)
            claim = do_the_work()
            h.result_ref = str(claim.pk)
            return claim

    The marker is written in the *caller's* transaction on purpose: if the work
    rolls back, so does the claim that it happened.
    """
    handle = _Handle()
    stamp = fingerprint(payload or {})

    existing = IdempotencyKey.objects.filter(key=key, scope=scope).first()
    if existing is not None:
        if existing.payload_fingerprint != stamp:
            raise IdempotencyConflict(
                f"Key {key!r} in scope {scope!r} was already used with a different "
                "payload. Reusing a key for different data would return the wrong "
                "result to one of the two callers."
            )
        handle.replayed = True
        handle.result_ref = existing.result_ref
        yield handle
        return

    try:
        with transaction.atomic():
            marker = IdempotencyKey.objects.create(
                key=key, scope=scope, payload_fingerprint=stamp
            )
            yield handle
            if handle.result_ref:
                marker.result_ref = handle.result_ref
                marker.save(update_fields=["result_ref"])
    except IntegrityError:
        # Lost a race with a concurrent identical request. The winner's work is
        # committed; report its result rather than retrying.
        winner = IdempotencyKey.objects.get(key=key, scope=scope)
        if winner.payload_fingerprint != stamp:
            raise IdempotencyConflict(
                f"Key {key!r} raced with a different payload in scope {scope!r}."
            ) from None
        handle.replayed = True
        handle.result_ref = winner.result_ref


class _Handle:
    __slots__ = ("replayed", "result_ref")

    def __init__(self) -> None:
        self.replayed = False
        self.result_ref = ""


def once(scope: str) -> Callable:
    """Decorator for service functions whose first argument is an idempotency key."""

    def wrap(fn):
        def inner(idempotency_key: str, *args, **kwargs):
            with idempotent(idempotency_key, scope=scope) as handle:
                if handle.replayed:
                    return None
                return fn(idempotency_key, *args, **kwargs)

        inner.__name__ = fn.__name__
        inner.__qualname__ = fn.__qualname__
        inner.__doc__ = fn.__doc__
        return inner

    return wrap

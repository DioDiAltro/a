"""Event subscriptions.

`charity` listens and never asks. The 3% is carved out by the ledger inside the
transaction that recognised the revenue; by the time this handler runs the money
is already in the pool, and all that is left to do is write down whose it is.
"""

from __future__ import annotations

import logging

from charity.services import record_allocation
from core.contracts import CharityAllocated
from core.events import bus

logger = logging.getLogger(__name__)

_registered = False


def on_charity_allocated(event: CharityAllocated) -> None:
    try:
        record_allocation(event)
    except Exception:
        # The carve-out itself already committed in the ledger. Losing the
        # record here means the public total under-reports, which is worth
        # shouting about but not worth crashing a post-commit hook over.
        logger.exception("Failed to record charity allocation %s", event.event_id)


def register() -> None:
    global _registered
    if _registered:
        return
    bus.subscribe(CharityAllocated, on_charity_allocated)
    _registered = True

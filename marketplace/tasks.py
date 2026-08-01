"""Scheduled work for the order engine.

`release_due_escrows` needs a beat entry in `config/celery.py`, which belongs to
another owner — noted in the handover. Until it has one, escrow still resolves
whenever the task is invoked, and nothing is lost in the meantime because the
release times are stored on the orders rather than computed at sweep time.
"""

from __future__ import annotations

import logging

from celery import shared_task

from marketplace.services import release_due_escrows

logger = logging.getLogger(__name__)


@shared_task(name="marketplace.tasks.release_due_escrows")
def release_due_escrows_task() -> dict[str, int]:
    """End the escrows whose windows have run out.

    A buyer who went quiet must not freeze the seller's money for ever, and a
    seller who never shipped must not keep the buyer's. Both directions are
    handled by the same sweep because both are the same failure: silence.
    """
    result = release_due_escrows()
    if result["paid"] or result["refunded"]:
        logger.info(
            "release_due_escrows paid=%s refunded=%s", result["paid"], result["refunded"]
        )
    return result

"""Scheduled work for the sale context.

`settle_due_sales` is referenced by name in `config/celery.py` and runs every
minute. That frequency is not caution — a lottery whose deadline passed ten
minutes ago is a user watching a page that says "estrazione in corso" and
wondering whether they have been robbed.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from salemodes.models import Sale
from salemodes.services import settle_due

logger = logging.getLogger(__name__)


@shared_task(name="salemodes.tasks.settle_due_sales")
def settle_due_sales() -> dict[str, int]:
    """Settle or abort every sale whose time is up.

    Two kinds of due, and the second is the anti-abuse half of the early draw:

    * the deadline passed — draw if the minimum is in, refund everybody to the
      exact scaglia if it is not;
    * an announced early draw's notice window elapsed — run it, whether or not
      the seller ever came back. An announcement the seller can quietly abandon
      would let them keep announcing until the ticket list suited them.
    """
    outcomes = settle_due()
    settled = sum(1 for outcome in outcomes if not outcome.aborted)
    aborted = sum(1 for outcome in outcomes if outcome.aborted)

    if outcomes:
        logger.info("settle_due_sales settled=%s aborted=%s", settled, aborted)

    return {"settled": settled, "aborted": aborted}


@shared_task(name="salemodes.tasks.expire_stale_announcements")
def expire_stale_announcements() -> int:
    """Safety net: log announced draws that somehow outlived their window.

    `settle_due_sales` should make this permanently empty. If it ever reports a
    non-zero count, a draw is late and somebody's money is sitting in a hold
    longer than they agreed to — worth an alert rather than a silent retry.
    """
    stale = Sale.objects.due(timezone.now()).filter(
        settlement_due_at__isnull=False
    ).count()

    if stale:
        logger.error("%s announced sale(s) are past their draw time and unsettled", stale)

    return stale

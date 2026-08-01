"""Scheduled work owned by the games context.

There is one job and it exists because of one rule: **a hold must never outlive
the thing it was placed for.** A challenge nobody answers would otherwise freeze
the challenger's stake forever, and "your money is locked up because somebody
you challenged never opened the app" is not a bug users forgive.

`wallet.tasks.release_expired_holds` is a second, independent backstop with a
slightly later deadline. Two sweepers with different owners is deliberate:
either one alone is a single point of failure for somebody's money.
"""

from __future__ import annotations

import logging

from celery import shared_task

from games.models import Duel
from games.services import expire_duel

logger = logging.getLogger(__name__)


@shared_task
def expire_stale_duels() -> int:
    """Release the stakes of every challenge that ran out of time.

    Each duel is expired in its own transaction so one wedged row cannot hold up
    the rest of the queue — a sweeper that gives up halfway leaves exactly the
    frozen holds it exists to prevent.
    """
    expired = 0

    for duel in Duel.objects.stale().iterator():
        try:
            expire_duel(duel)
            expired += 1
        except Exception:
            logger.exception("Could not expire duel %s", duel.pk)

    if expired:
        logger.info("Expired %d stale duel(s)", expired)
    return expired

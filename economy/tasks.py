"""Scheduled work. `config/celery.py` already points at the first two by name.

Thin on purpose: a Celery task is a scheduling detail, and everything it does
must also be runnable from a shell, a test or a management command without a
broker in the room. The logic lives in `economy.services` and
`economy.monitoring`.
"""

from __future__ import annotations

import logging

from celery import shared_task

from economy.monitoring import inflation_alert, take_snapshot
from economy.services import audit_rtp, run_drip

logger = logging.getLogger("economy")


@shared_task(name="economy.tasks.run_passive_drip")
def run_passive_drip() -> dict:
    """Rain a few scaglie on some idle accounts.

    Runs every five minutes. Pays out of the redistribution pool through
    `wallet.services.drip`, which is what makes it redistribution rather than
    issuance: when the pool is dry this is a no-op, and that is the correct
    behaviour rather than a degraded one.
    """
    summary = run_drip()
    if summary["skipped"]:
        logger.debug("Passive drip skipped: %s", summary["skipped"])
    return summary


@shared_task(name="economy.tasks.audit_realised_rtp")
def audit_realised_rtp() -> list[dict]:
    """Compare what the games paid against what they promise.

    Every six hours. A drift is treated as an incident, not a metric: the
    outcome tables are the promise made to the player, and the alert is logged
    at ERROR so it reaches whoever is on call.
    """
    audits = audit_rtp()
    return [
        {
            "game_key": audit.game_key,
            "rounds": audit.rounds,
            "realised_bp": audit.realised_bp,
            "configured_bp": audit.configured_bp,
            "drift_bp": audit.drift_bp,
            "is_alert": audit.is_alert,
        }
        for audit in audits
    ]


@shared_task(name="economy.tasks.take_economy_snapshot")
def take_economy_snapshot() -> dict:
    """Photograph sources against sinks and raise the alarm if they diverge.

    Not in the beat schedule yet — add it hourly when somebody owns the alert.
    """
    snapshot = take_snapshot()
    return {
        "id": snapshot.id,
        "taken_at": snapshot.taken_at.isoformat(),
        "net_to_users_minor": snapshot.net_to_users_minor,
        "alert": snapshot.alert,
    }


@shared_task(name="economy.tasks.check_inflation")
def check_inflation() -> dict | None:
    alert = inflation_alert()
    if alert:
        return {"code": alert["code"], "message": alert["message"], "detail": alert["detail"]}
    return None

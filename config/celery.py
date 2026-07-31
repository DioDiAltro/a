import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("biglie")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # Lotteries whose deadline has passed either draw or refund. Runs often
    # because a late draw is a support ticket.
    "settle-due-sales": {
        "task": "salemodes.tasks.settle_due_sales",
        "schedule": crontab(minute="*"),
    },
    # The passive drip pays out of the redistribution pool, never out of thin
    # air. If the pool is empty this task is a no-op.
    "run-passive-drip": {
        "task": "economy.tasks.run_passive_drip",
        "schedule": crontab(minute="*/5"),
    },
    # Unclaimed credits expire into the redistribution pool.
    "expire-stale-pending-credits": {
        "task": "wallet.tasks.expire_stale_pending_credits",
        "schedule": crontab(hour=3, minute=0),
    },
    # Compare realised RTP against configured RTP per game; alerts on drift.
    "audit-realised-rtp": {
        "task": "economy.tasks.audit_realised_rtp",
        "schedule": crontab(hour="*/6", minute=15),
    },
    "release-expired-holds": {
        "task": "wallet.tasks.release_expired_holds",
        "schedule": crontab(minute="*/10"),
    },
}

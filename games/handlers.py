"""Event subscriptions owned by `games`.

Registered from `GamesConfig.ready()`. There is one, and `core.contracts` asks
for it explicitly: `SelfExclusionStarted` says handlers are expected to *refund
staked wagers*, not merely block login. An open duel is a staked wager and, on
the defender's side, an outstanding invitation to gamble — leaving either in
place would keep soliciting somebody who has just asked to be left alone.
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model

from core.contracts import SelfExclusionStarted
from core.events import bus
from games.services import cancel_open_duels_for

logger = logging.getLogger(__name__)


def on_self_exclusion(event: SelfExclusionStarted) -> None:
    user = get_user_model().objects.filter(pk=event.user_id).first()
    if user is None:
        return
    cancel_open_duels_for(user)


_registered = False


def register() -> None:
    global _registered
    if _registered:
        return
    bus.subscribe(SelfExclusionStarted, on_self_exclusion)
    _registered = True

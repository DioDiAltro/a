"""The internal event bus — the seam that keeps bounded contexts apart.

Contexts never import each other's models. When `salemodes` finishes a lottery
it publishes `LotteryDrawn`; `wallet`, `charity` and `realtime` react. Nobody
holds a reference to anybody.

Two delivery modes, and choosing wrongly is the classic source of "the money
moved but the email never sent" bugs:

* ``publish()`` — handlers run **after the current transaction commits**. This
  is the default and what you almost always want for side effects: notifying,
  emailing, pushing over WebSocket. If the transaction rolls back, they never
  ran, because the thing they describe never happened.

* ``publish_now()`` — handlers run **inside** the caller's transaction. Use only
  when the handler's own writes must be atomic with the trigger, e.g. the
  charity split must commit with the sale or not at all.

This module is deliberately in-process. When a context is extracted into its own
service, `publish` becomes the place a broker adapter is plugged in and no
domain code changes.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from django.db import transaction

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base class for events. Subclasses are frozen dataclasses of plain data.

    Never put a model instance on an event. Events must stay serialisable so
    that moving to a real broker later is a transport change, not a rewrite.
    """

    #: Set on each subclass; used for logging and for broker routing keys.
    name: ClassVar[str] = "domain.event"

    event_id: UUID = field(default_factory=uuid4, kw_only=True)
    occurred_at: datetime = field(default_factory=datetime.now, kw_only=True)


Handler = Callable[[Any], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: type, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def handler(self, event_type: type) -> Callable[[Handler], Handler]:
        """Decorator form: ``@bus.handler(LotteryDrawn)``."""

        def register(fn: Handler) -> Handler:
            self.subscribe(event_type, fn)
            return fn

        return register

    def _handlers_for(self, event: DomainEvent) -> list[Handler]:
        # Walk the MRO so a handler on a base event type sees its subclasses.
        found: list[Handler] = []
        for klass in type(event).__mro__:
            found.extend(self._handlers.get(klass, ()))
        return found

    def publish(self, event: DomainEvent) -> None:
        """Deliver after the current transaction commits (or immediately if none)."""
        transaction.on_commit(lambda: self._dispatch(event))

    def publish_now(self, event: DomainEvent) -> None:
        """Deliver synchronously, inside the caller's transaction."""
        self._dispatch(event, reraise=True)

    def _dispatch(self, event: DomainEvent, *, reraise: bool = False) -> None:
        for handler in self._handlers_for(event):
            try:
                handler(event)
            except Exception:
                # A failing notification handler must not undo a settled sale.
                # Synchronous handlers are inside the money transaction, so
                # there we do the opposite and let it blow up.
                logger.exception(
                    "Handler %s failed for %s (event_id=%s)",
                    getattr(handler, "__qualname__", handler),
                    event.name,
                    event.event_id,
                )
                if reraise:
                    raise

    def reset(self) -> None:
        """Drop all subscriptions. Tests only."""
        self._handlers.clear()


bus = EventBus()

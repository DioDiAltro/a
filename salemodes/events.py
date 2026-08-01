"""Events that belong to this context alone.

Anything that crosses a boundary lives in `core.contracts`. What is here is the
sale context talking to itself and to the realtime layer.

`EarlyDrawAnnounced` is the odd one out and worth explaining: it *should* be a
cross-context event, because the announcement is a broadcast — the whole
anti-abuse argument rests on every participant being told that a draw is coming
and having time to react. `core.contracts` has no event for it, so it lives
here until one is added. See the note in the handover report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from core.events import DomainEvent
from core.money import Asset


@dataclass(frozen=True, slots=True)
class EarlyDrawAnnounced(DomainEvent):
    """A seller committed publicly to drawing early.

    Binding: the draw happens at `draw_at` whether or not the seller comes
    back. Carries the pot so a notification can say what is at stake without
    querying back into this context.
    """

    name: ClassVar[str] = "sale.early_draw_announced"

    sale_id: UUID
    listing_id: UUID
    seller_id: UUID
    draw_at: datetime
    total_minor: int
    asset: Asset
    #: Published before the draw; the seed behind it comes out afterwards.
    commitment_hash: str


@dataclass(frozen=True, slots=True)
class ParticipationRefused(DomainEvent):
    """A participation was reserved and immediately given back.

    Emitted by mechanics that resolve on the spot (a lost challenge). The money
    never left the participant, but the attempt is worth recording: a mode that
    refuses everybody is a bug, and this is how it becomes visible.
    """

    name: ClassVar[str] = "sale.participation_refused"

    sale_id: UUID
    user_id: UUID
    reason: str

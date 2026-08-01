"""Where the marketplace listens.

Kept as its own module so the wiring is one short file you can read in full and
know every way an event can reach the order engine.

`SaleSettled` is subscribed rather than called: `salemodes` settles a sale
knowing nothing about orders, escrow or goods. That is what lets a new sale
mechanic ship without this file changing — and what would let the sale context
move into its own service with only this subscription becoming a queue.
"""

from __future__ import annotations

from core.contracts import SaleAborted, SaleSettled
from core.events import bus
from marketplace.services import on_sale_aborted, on_sale_settled

bus.subscribe(SaleSettled, on_sale_settled)
bus.subscribe(SaleAborted, on_sale_aborted)

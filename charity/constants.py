"""The charity context's vocabulary.

Public: other contexts and the presentation layer may import these names to talk
about payout states without reaching into `charity.models`.
"""

from __future__ import annotations

from django.db import models


class PayoutState(models.TextChoices):
    """Where a payout is in its life.

    `INSTRUCTED` is the honest name for the first state. The ledger's
    CHARITY_POOL balance is money the platform has already set aside and owes to
    a beneficiary; a row in this state says "this slice is earmarked for this
    organisation", not "this money has arrived". Only `PAID` — which carries the
    transfer reference — claims that.
    """

    INSTRUCTED = "instructed", "Da versare"
    PAID = "paid", "Versato"
    FAILED = "failed", "Fallito"
    CANCELLED = "cancelled", "Annullato"


#: States that still count against the pool: an instructed payout is spoken for,
#: so a second distribution must not hand the same scaglie to somebody else.
COMMITTED_STATES = (PayoutState.INSTRUCTED, PayoutState.PAID)

#: Default weight for a new beneficiary. Weights are relative, so the absolute
#: number means nothing on its own; 100 leaves room to say "half as much" without
#: fractions.
DEFAULT_WEIGHT = 100

#: Below this the pool is not worth a distribution run — a bank transfer of four
#: scaglie costs more to make than it is worth. Once a run *does* happen it
#: distributes the whole pool, to the last minor unit.
MIN_DISTRIBUTION_MINOR = 1_000  # 10 biglie

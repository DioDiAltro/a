"""The vocabulary of the compliance context.

This is a public submodule: other contexts may import these names to talk about
KYC tiers and limit kinds without reaching into `compliance.models`.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import models


class KycTier(models.TextChoices):
    """How much we know about who this person is.

    The tiers are cumulative and the ordering matters, so compare through
    `tier_rank` rather than by string.
    """

    NONE = "none", "Nessuna verifica"
    #: Identity document seen and matched to the account. Enough to withdraw.
    BASIC = "basic", "Verifica base"
    #: Document plus proof of address and source of funds. Required once the
    #: cumulative amount leaving the platform stops looking like pocket money.
    FULL = "full", "Verifica completa"


TIER_RANK: dict[str, int] = {KycTier.NONE: 0, KycTier.BASIC: 1, KycTier.FULL: 2}


def tier_rank(tier: str) -> int:
    return TIER_RANK.get(str(tier), 0)


def tier_at_least(tier: str, minimum: str) -> bool:
    return tier_rank(tier) >= tier_rank(minimum)


class KycStatus(models.TextChoices):
    PENDING = "pending", "In revisione"
    APPROVED = "approved", "Approvata"
    REJECTED = "rejected", "Respinta"
    EXPIRED = "expired", "Scaduta"


class DocumentType(models.TextChoices):
    ID_CARD = "id_card", "Carta d'identità"
    PASSPORT = "passport", "Passaporto"
    DRIVING_LICENCE = "driving_licence", "Patente"
    PROOF_OF_ADDRESS = "proof_of_address", "Prova di residenza"
    SOURCE_OF_FUNDS = "source_of_funds", "Origine dei fondi"


class LimitKind(models.TextChoices):
    """What a self-imposed limit counts.

    DEPOSIT, LOSS and WAGER count minor units of money. SESSION_TIME counts
    minutes — the odd one out, and the reason `check_limit` takes either an
    `Amount` or a plain integer.
    """

    DEPOSIT = "deposit", "Deposito"
    LOSS = "loss", "Perdita"
    WAGER = "wager", "Giocato"
    SESSION_TIME = "session_time", "Tempo di gioco"

    #: Recorded, never enforced. Withdrawals are tracked because the cumulative
    #: figure decides when BASIC verification stops being enough — but a *limit*
    #: on getting your own money out would protect nobody and trap somebody, so
    #: this kind is deliberately absent from MONEY_LIMIT_KINDS and `check_limit`
    #: refuses it.
    WITHDRAWAL = "withdrawal", "Prelievo"


#: The kinds a user may cap and `check_limit` will enforce. WITHDRAWAL is not
#: here on purpose: see above.
MONEY_LIMIT_KINDS = frozenset({LimitKind.DEPOSIT, LimitKind.LOSS, LimitKind.WAGER})

#: Kinds `record_usage` accepts. Wider than MONEY_LIMIT_KINDS because tracking
#: and capping are different questions.
TRACKED_LIMIT_KINDS = MONEY_LIMIT_KINDS | {LimitKind.WITHDRAWAL}


class LimitPeriod(models.TextChoices):
    DAY = "day", "Giornaliero"
    WEEK = "week", "Settimanale"
    MONTH = "month", "Mensile"


#: Rolling windows, not calendar ones. A calendar limit resets at midnight and
#: hands a user who just hit their daily cap a fresh one a few minutes later,
#: which is precisely the moment the limit exists to protect.
PERIOD_WINDOW: dict[str, timedelta] = {
    LimitPeriod.DAY: timedelta(days=1),
    LimitPeriod.WEEK: timedelta(days=7),
    LimitPeriod.MONTH: timedelta(days=30),
}

#: A limit may always be tightened at once. Loosening waits this long, so the
#: decision is made by the person who woke up tomorrow rather than by the person
#: who is losing right now.
COOLING_OFF = timedelta(hours=24)


class ExclusionState(models.TextChoices):
    ACTIVE = "active", "Attiva"
    EXPIRED = "expired", "Terminata"


#: Offered durations for self-exclusion. Permanent is the empty value.
EXCLUSION_DURATIONS: tuple[tuple[str, str], ...] = (
    ("1", "24 ore"),
    ("7", "7 giorni"),
    ("30", "30 giorni"),
    ("90", "3 mesi"),
    ("180", "6 mesi"),
    ("365", "1 anno"),
    ("permanent", "Per sempre"),
)

# ---------------------------------------------------------------------------
# KYC thresholds
# ---------------------------------------------------------------------------

#: Any withdrawal at all requires at least this tier.
WITHDRAWAL_MINIMUM_TIER = KycTier.BASIC

#: Once the cumulative amount a user has taken off the platform passes this, the
#: relationship is no longer small change and enhanced due diligence applies.
#: €2000 in cents.
FULL_KYC_CUMULATIVE_THRESHOLD_MINOR = 200_000

# ---------------------------------------------------------------------------
# Reality check
# ---------------------------------------------------------------------------

DEFAULT_REALITY_CHECK_MINUTES = 60
MIN_REALITY_CHECK_MINUTES = 15
MAX_REALITY_CHECK_MINUTES = 240

#: Session keys used by `services.reality_check`. Namespaced so they cannot
#: collide with anything another context puts in the session.
SESSION_STARTED_KEY = "compliance:session_started_at"
LAST_REALITY_CHECK_KEY = "compliance:last_reality_check_at"

# ---------------------------------------------------------------------------
# Audit journal
# ---------------------------------------------------------------------------

#: The hash the first row chains from. Any value would do; a fixed constant
#: makes an empty journal verifiable too.
GENESIS_HASH = "0" * 64

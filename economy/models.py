"""The economy's own books: what was earned, what rained, what the games paid.

None of this is authoritative about money — the ledger is. These rows are the
*surveillance* copy, accumulated from events, and they exist because the
questions this context has to answer ("is the currency inflating", "is that slot
paying what it promises") span contexts that must not import each other.

Deliberately no ForeignKey to `wallet`: `economy` may call `wallet.services` and
read `wallet.selectors`, and that is the whole of the relationship. Ids are
stored as plain UUIDs.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.money import Amount, Asset

ASSET_CHOICES = [(a, a.label) for a in Asset]


class RevenueRecord(models.Model):
    """One `RevenueRecognised` event.

    The two carve-outs are recomputed and stored alongside rather than read back
    from the ledger, so a drift between what the constants say and what the
    ledger did shows up as a mismatch instead of being papered over by reading
    the same source twice.
    """

    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(unique=True, null=True, blank=True)

    amount_minor = models.BigIntegerField()
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES, db_index=True)
    source = models.CharField(max_length=60, db_index=True)
    reference_id = models.CharField(max_length=64, blank=True)

    #: Derived at record time from the constants in `core.contracts`.
    charity_minor = models.BigIntegerField(default=0)
    redistribution_minor = models.BigIntegerField(default=0)

    occurred_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ("-occurred_at",)
        indexes = [models.Index(fields=["asset", "occurred_at"])]

    def __str__(self) -> str:
        return f"{self.amount} da {self.source}"

    @property
    def amount(self) -> Amount:
        return Amount(self.amount_minor, Asset(self.asset))


class MintRecord(models.Model):
    """One `DepositConfirmed` event — the only event that creates biglie.

    Kept because `economy` cannot read the ledger's ISSUANCE account (the
    boundary test forbids importing `wallet.ledger`) and supply surveillance
    needs a denominator. Every row here is new currency; nothing else is.
    """

    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(unique=True, null=True, blank=True)

    deposit_id = models.UUIDField()
    user_id = models.UUIDField(null=True, blank=True, db_index=True)
    euro_minor = models.BigIntegerField()
    biglie_minor = models.BigIntegerField()
    occurred_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ("-occurred_at",)

    def __str__(self) -> str:
        return f"conio {self.biglie_minor} da deposito {self.euro_minor}"


class WithdrawalRecord(models.Model):
    """One `WithdrawalRequested` event.

    Requested, not settled: the contract has no completion event, so this is a
    lower bound on what has left. Supply figures derived from it say so.
    """

    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(unique=True, null=True, blank=True)

    withdrawal_id = models.UUIDField()
    user_id = models.UUIDField(null=True, blank=True, db_index=True)
    euro_minor = models.BigIntegerField()
    occurred_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ("-occurred_at",)

    def __str__(self) -> str:
        return f"prelievo richiesto {self.euro_minor}"


class GameRoundOutcome(models.Model):
    """A settled round, stripped to the two numbers RTP is made of.

    Fed by `GameSettled` and by the risked half of `CreditClaimed`: the claim
    gamble is a game with the same edge, and leaving it out of the watchdog
    would leave the most-played surface on the platform unwatched.
    """

    class Origin(models.TextChoices):
        MINIGAME = "minigame", "Minigioco"
        CLAIM = "claim", "Riscatto rischioso"

    id = models.BigAutoField(primary_key=True)
    round_id = models.UUIDField(unique=True)
    game_key = models.CharField(max_length=40, db_index=True)
    origin = models.CharField(max_length=10, choices=Origin.choices, default=Origin.MINIGAME)

    user_id = models.UUIDField(null=True, blank=True, db_index=True)
    stake_minor = models.BigIntegerField()
    payout_minor = models.BigIntegerField()
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)
    settled_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ("-settled_at",)
        indexes = [models.Index(fields=["game_key", "settled_at"])]

    def __str__(self) -> str:
        return f"{self.game_key} {self.stake_minor}→{self.payout_minor}"

    @property
    def margin_minor(self) -> int:
        """What the house kept. Negative on a round the player won."""
        return self.stake_minor - self.payout_minor


class DripAward(models.Model):
    """One passive award, written by the run that made it.

    Written here rather than derived from `DripAwarded` events because the run
    needs to read its own history back immediately — the per-user daily cap is
    enforced against these rows — and because the run id and the weighting that
    chose this user are worth keeping for the day somebody asks whether the
    "random" reward was actually random.
    """

    id = models.BigAutoField(primary_key=True)
    run_id = models.UUIDField(db_index=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="drip_awards"
    )
    amount_minor = models.BigIntegerField()
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)
    #: The pending credit the wallet created. The user still has to claim it.
    credit_id = models.UUIDField(null=True, blank=True)
    #: Selection weight at draw time — the audit trail of the randomness.
    weight = models.PositiveIntegerField(default=1)
    awarded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-awarded_at",)
        indexes = [models.Index(fields=["user", "awarded_at"])]

    def __str__(self) -> str:
        return f"{self.amount} a {self.user_id}"

    @property
    def amount(self) -> Amount:
        return Amount(self.amount_minor, Asset(self.asset))


class RtpAudit(models.Model):
    """The verdict of one surveillance pass over one game.

    Kept even when nothing is wrong: a table of "fine, fine, fine, drift" is
    what makes the drift legible, and an alert with no history behind it is an
    argument rather than evidence.
    """

    id = models.BigAutoField(primary_key=True)
    game_key = models.CharField(max_length=40, db_index=True)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField(db_index=True)

    rounds = models.PositiveIntegerField()
    staked_minor = models.BigIntegerField()
    paid_minor = models.BigIntegerField()

    realised_bp = models.IntegerField()
    configured_bp = models.IntegerField()
    #: realised - configured. Negative means the game is paying less than
    #: promised, which is the direction that is a promise broken to the player.
    drift_bp = models.IntegerField()
    is_alert = models.BooleanField(default=False, db_index=True)
    note = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-window_end", "game_key")
        indexes = [models.Index(fields=["game_key", "window_end"])]

    def __str__(self) -> str:
        return f"{self.game_key} {self.realised_bp}bp vs {self.configured_bp}bp"

    @property
    def realised_percent(self) -> float:
        return self.realised_bp / 100

    @property
    def configured_percent(self) -> float:
        return self.configured_bp / 100


class EconomySnapshot(models.Model):
    """A periodic photograph of sources against sinks.

    One row per pass, so the shape of the economy over time is a query rather
    than an archaeology project. The headline figures sum across biglia
    flavours: a display convenience, exactly as in `wallet.services.summary_for`,
    and never arithmetic anything relies on.
    """

    id = models.BigAutoField(primary_key=True)
    taken_at = models.DateTimeField(default=timezone.now, db_index=True)
    window_hours = models.PositiveIntegerField()

    # sources — value arriving in users' hands
    minted_minor = models.BigIntegerField(default=0)
    house_paid_minor = models.BigIntegerField(default=0)
    drip_minor = models.BigIntegerField(default=0)

    # sinks — value returning to the house
    revenue_minor = models.BigIntegerField(default=0)
    charity_minor = models.BigIntegerField(default=0)
    redistribution_minor = models.BigIntegerField(default=0)

    # standing balances
    charity_pool_minor = models.BigIntegerField(default=0)
    redistribution_pool_minor = models.BigIntegerField(default=0)

    alert = models.CharField(max_length=40, blank=True, db_index=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-taken_at",)

    def __str__(self) -> str:
        return f"snapshot {self.taken_at:%Y-%m-%d %H:%M}"

    @property
    def net_to_users_minor(self) -> int:
        """Sources minus sinks. Positive means value flowed toward players."""
        return self.minted_minor + self.house_paid_minor + self.drip_minor - self.revenue_minor

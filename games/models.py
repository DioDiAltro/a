"""What the games context remembers.

Two aggregates. `GameRound` is one settled (or settling) play; `Duel` is the
negotiation between two sellers that produces one.

Both hold references to `wallet` objects as **bare UUIDs**, never foreign keys.
A hold and a pending credit belong to the ledger, and a database-level link from
a game table into the ledger would let a games migration take the money with it.
The ledger answers questions; it does not get pointed at.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.contracts import DUEL_RAKE_BASIS_POINTS
from core.models import AmountField, BaseModel
from core.money import Amount, Asset, split_percentage
from games.constants import DuelState, GameKey, RoundSource, RoundState

# `FairnessProof` lives next to the RNG in `fairness.py`, because the commit
# reveal scheme and the row that records it are one idea and reading them apart
# helps nobody. Django only discovers models that `models.py` imports, so it is
# re-exported here — that import is load-bearing, not tidying.
from games.fairness import FairnessProof  # noqa: F401  (registers the model)

ASSET_CHOICES = [(a.value, a.label) for a in Asset]


class GameRoundQuerySet(models.QuerySet):
    def history(self, user):
        return (
            self.filter(user=user)
            .exclude(state=RoundState.CANCELLED)
            .select_related("proof")
            .order_by("-created_at")
        )

    def live_ladder(self, user):
        return self.filter(
            user=user, game_key=GameKey.LADDER, state=RoundState.IN_PROGRESS
        )


class GameRound(BaseModel):
    """One play, and everything needed to prove it was not tampered with.

    The row is written **before** the money moves, in the same transaction, so a
    settlement that crashes halfway leaves a visible `COMMITTED` round rather
    than a silent gap. A player who can see a round with no outcome can ask
    about it; a player with no row at all cannot.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="game_rounds"
    )
    game_key = models.CharField(max_length=16, choices=[(k, k) for k in GameKey], db_index=True)
    source = models.CharField(
        max_length=16, choices=[(s, s) for s in RoundSource], default=RoundSource.CLAIM_GAMBLE
    )
    state = models.CharField(
        max_length=16, choices=[(s, s) for s in RoundState],
        default=RoundState.COMMITTED, db_index=True,
    )

    stake_minor = AmountField()
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)

    #: Null until settled. Zero is a real, common and different answer.
    payout_minor = AmountField(null=True, blank=True)
    multiplier_bp = models.PositiveIntegerField(null=True, blank=True)

    #: The commitment this round was derived from. PROTECT: a round without its
    #: proof is unverifiable, which would make the fairness claim retroactively
    #: false for everyone who already played.
    proof = models.ForeignKey(
        "games.FairnessProof", on_delete=models.PROTECT, related_name="rounds"
    )

    #: The full derived outcome, in the shape `engine.Outcome.as_json()` returns.
    #: For the ladder this contains the entire climb from the first roll, which
    #: is why `public_detail()` exists and templates must never render `outcome`
    #: directly.
    outcome = models.JSONField(default=dict, blank=True)

    #: The pending credit that was gambled, when this round came from a claim.
    #: A UUID, not an FK — see the module docstring.
    credit_id = models.UUIDField(null=True, blank=True, db_index=True)

    settled_at = models.DateTimeField(null=True, blank=True)

    objects = GameRoundQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["game_key", "created_at"]),
            models.Index(fields=["state", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(stake_minor__gt=0), name="round_stake_positive"),
            models.CheckConstraint(
                check=Q(payout_minor__isnull=True) | Q(payout_minor__gte=0),
                name="round_payout_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.game_key} round {self.id}"

    # -- money --------------------------------------------------------------

    @property
    def stake(self) -> Amount:
        return Amount(self.stake_minor, Asset(self.asset))

    @property
    def payout(self) -> Amount:
        return Amount(self.payout_minor or 0, Asset(self.asset))

    @property
    def is_win(self) -> bool:
        return bool(self.payout_minor)

    @property
    def net_minor(self) -> int:
        """What the player is up or down on this round."""
        return (self.payout_minor or 0) - self.stake_minor

    # -- presentation -------------------------------------------------------

    @property
    def is_settled(self) -> bool:
        return self.state == RoundState.SETTLED

    def public_detail(self) -> dict:
        """The outcome detail with anything unrevealed removed.

        The ladder's entire path is decided at the first roll, so serving the
        raw detail to a live round would hand the player the answer. Everything
        is revealed once the round settles — at that point hiding it would only
        obstruct verification.
        """
        detail = dict(self.outcome.get("detail") or {})
        if self.game_key == GameKey.LADDER and not self.is_settled:
            revealed = int(detail.get("revealed_rungs") or 0)
            detail["path"] = list(detail.get("path") or [])[:revealed]
            detail["fell_at"] = None
        return detail


class DuelQuerySet(models.QuerySet):
    def open_for(self, user):
        return self.filter(
            Q(challenger=user) | Q(defender=user), state=DuelState.PENDING
        ).order_by("expires_at")

    def resolved_for(self, user):
        return (
            self.filter(Q(challenger=user) | Q(defender=user), state=DuelState.RESOLVED)
            .select_related("challenger", "defender", "winner", "round")
            .order_by("-resolved_at")
        )

    def stale(self):
        """Pending duels past their deadline. The sweeper's input."""
        return self.filter(state=DuelState.PENDING, expires_at__lte=timezone.now())

    def recent_between(self, challenger, defender, since):
        """Used by the anti-harassment check, and only by it."""
        return self.filter(challenger=challenger, defender=defender, created_at__gte=since)


class Duel(BaseModel):
    """A seller staking biglie against another seller's.

    Both stakes sit in `wallet` holds until the duel resolves, which is what
    makes the mechanic safe to leave pending: the money is reserved but nobody
    has lost it, and every terminal state either releases both holds or captures
    both. There is no path that captures one and releases the other.
    """

    challenger = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="duels_started"
    )
    defender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="duels_received"
    )

    #: Each side stakes this. The pot is twice it.
    stake_minor = AmountField()
    asset = models.CharField(max_length=16, choices=ASSET_CHOICES)

    state = models.CharField(
        max_length=16, choices=[(s, s) for s in DuelState],
        default=DuelState.PENDING, db_index=True,
    )

    #: A challenge nobody answers must not freeze the challenger's stake
    #: indefinitely. `games.tasks.expire_stale_duels` sweeps on this column.
    expires_at = models.DateTimeField(db_index=True)

    message = models.CharField(max_length=140, blank=True)

    winner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="duels_won",
    )
    round = models.OneToOneField(
        GameRound, null=True, blank=True, on_delete=models.PROTECT, related_name="duel"
    )

    #: Committed when the challenge is sent, so the hash is published before the
    #: defender has said anything — and bound to *their* client seed when they
    #: accept. Neither seat can steer the result: the challenger does not know
    #: the seed the defender will pick, and the defender cannot see a server
    #: seed that was committed before they were asked.
    proof = models.ForeignKey(
        "games.FairnessProof", null=True, blank=True,
        on_delete=models.PROTECT, related_name="duels",
    )

    #: Wallet hold ids. UUIDs, not FKs — see the module docstring.
    challenger_hold_id = models.UUIDField(null=True, blank=True)
    defender_hold_id = models.UUIDField(null=True, blank=True)

    rake_minor = AmountField(default=0)
    resolved_at = models.DateTimeField(null=True, blank=True)

    objects = DuelQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["defender", "state", "expires_at"]),
            models.Index(fields=["challenger", "state"]),
            models.Index(fields=["challenger", "defender", "created_at"]),
            models.Index(fields=["state", "expires_at"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(stake_minor__gt=0), name="duel_stake_positive"),
            models.CheckConstraint(
                check=~Q(challenger=models.F("defender")), name="duel_needs_two_people"
            ),
        ]

    def __str__(self) -> str:
        return f"duel {self.challenger_id} vs {self.defender_id} for {self.stake}"

    # -- money --------------------------------------------------------------

    @property
    def stake(self) -> Amount:
        return Amount(self.stake_minor, Asset(self.asset))

    @property
    def pot(self) -> Amount:
        """Both stakes. What the two of them are actually playing for."""
        return Amount(self.stake_minor * 2, Asset(self.asset))

    def split_pot(self) -> tuple[Amount, Amount]:
        """`(rake, to_winner)`, summing to exactly the pot.

        Delegates to `core.money.split_percentage`, whose remainder-absorbing
        behaviour is what guarantees the two halves add back up. A duel that
        loses a scaglia to rounding is a duel where the ledger has to invent one.
        """
        return split_percentage(self.pot, DUEL_RAKE_BASIS_POINTS)

    # -- state --------------------------------------------------------------

    @property
    def is_pending(self) -> bool:
        return self.state == DuelState.PENDING

    @property
    def is_expired(self) -> bool:
        return self.is_pending and self.expires_at <= timezone.now()

    def loser_id(self):
        if self.winner_id is None:
            return None
        return self.defender_id if self.winner_id == self.challenger_id else self.challenger_id

    def opponent_of(self, user):
        return self.defender if user.pk == self.challenger_id else self.challenger

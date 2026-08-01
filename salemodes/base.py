"""The sale-mode plugin contract.

This is the architectural centrepiece of the marketplace. The brief asks for
endless inventiveness in *how* things are sold — lotteries, auctions, group
buys, sealed boxes, "beat me at a game and I'll give you a discount" — and the
only way to keep that promise without the order engine rotting is to make the
mechanic a plugin and the engine indifferent to which one is running.

The seam
--------
`marketplace` never asks "is this a lottery?". It asks the registry for a mode,
hands it a `Sale`, and reacts to the events that come back. A new mechanic is a
new subclass of `SaleMode` plus a template partial. Nothing in the order engine
changes — there is a test that proves exactly this by registering a mode that
does not exist in this package and driving it end to end.

The contract
------------
Every hook returns an explicit result object. None of them mutate the ledger:
money moves only through `wallet.services`, reached via the helpers in
`salemodes.holds`, so there is exactly one place where a sale can touch a
balance and it is auditable.

* ``validate_config(config)`` — normalise and reject at listing time, loudly and
  in Italian. A misconfigured sale must be impossible to open, not merely
  impossible to settle.
* ``can_start(sale)`` — a last gate before the sale goes live.
* ``on_participation(sale, user, amount, units)`` — the mechanic itself: accept
  the bid/ticket/slot, place the hold, release whatever it supersedes.
* ``can_settle_early(sale)`` — whether the seller may close before the deadline.
  This is where anti-abuse policy lives for the lottery.
* ``settle(sale)`` — decide who won and capture the money.
* ``refund(sale)`` — give everything back, to the scaglia.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar
from uuid import UUID

from core.exceptions import SaleModeError
from core.money import Amount
from salemodes.constants import DEFAULT_SALE_DURATION_DAYS, FeeScope

# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decision:
    """A yes/no with a reason the user can read.

    Modes return this instead of a bare bool so that every refusal arrives with
    an Italian explanation attached. "False" is not a message.
    """

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed

    @classmethod
    def yes(cls, reason: str = "") -> Decision:
        return cls(True, reason)

    @classmethod
    def no(cls, reason: str) -> Decision:
        return cls(False, reason)

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise SaleModeError(self.reason)


@dataclass(frozen=True, slots=True)
class ParticipationResult:
    """What happened when somebody tried to join a sale."""

    accepted: bool
    #: The `Participation` row, when one was created.
    participation: Any = None
    #: Shown to the participant. Italian.
    message: str = ""
    #: The mode noticed this participation crossed the mode's own threshold.
    #: The service turns it into `SaleThresholdReached`.
    threshold_reached: bool = False
    #: Buy-it-now, a won challenge, a sold-out box: settle without waiting.
    settle_now: bool = False
    #: Anti-sniping pushed the close out to here.
    extended_to: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def refused(cls, reason: str) -> ParticipationResult:
        return cls(accepted=False, message=reason)


@dataclass(frozen=True, slots=True)
class Award:
    """One buyer's share of a settlement.

    A list rather than a single winner because a group buy settles to everybody
    who joined and a mystery box settles to everybody who opened one. Modes with
    a single winner simply return a one-element tuple.
    """

    user_id: UUID
    gross: Amount
    #: Mode-specific detail carried into the order — the prize drawn from a
    #: sealed box, the discount earned in a challenge.
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SettlementResult:
    """The outcome of a sale. Consumed by `salemodes.services.settle_sale`."""

    awards: tuple[Award, ...] = ()
    #: Set for any mode whose outcome is drawn: lets anyone replay the draw.
    fairness_proof_id: UUID | None = None
    aborted: bool = False
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def abort(cls, reason: str) -> SettlementResult:
        return cls(aborted=True, reason=reason)

    @property
    def winner_id(self) -> UUID | None:
        """The headline winner, for the single-winner modes and the UI."""
        return self.awards[0].user_id if self.awards else None


@dataclass(frozen=True, slots=True)
class RefundResult:
    """What a refund gave back. `total_minor` is asserted against in tests."""

    refunded_count: int = 0
    total_minor: int = 0
    reason: str = ""


# ---------------------------------------------------------------------------
# The plugin
# ---------------------------------------------------------------------------


class SaleMode(abc.ABC):
    """One way of selling something.

    Subclasses are stateless singletons: the registry instantiates once and the
    instance is shared, so a mode must keep every scrap of state on the `Sale`
    row it is handed. That is what lets the same mode serve a thousand
    concurrent sales and what makes a sale replayable from the database alone.
    """

    #: Registry key. Stored on `Sale.mode` and used in URLs.
    key: ClassVar[str] = ""
    #: User-facing name, Italian.
    label: ClassVar[str] = ""
    #: One sentence on how it works, Italian. Shown in the listing form.
    description: ClassVar[str] = ""

    #: Template partial the listing page includes to render mode-specific UI.
    #: This is the other half of the plugin seam: the order engine renders a
    #: name the mode chose, so a new mechanic ships its own interface.
    template_partial: ClassVar[str] = "salemodes/modes/_generic.html"

    #: Whether every participant pays (POOL) or only the winner does (WINNER).
    #: Drives how the marketplace commission is apportioned across holds.
    fee_scope: ClassVar[FeeScope] = FeeScope.WINNER

    #: A mode that has no deadline (buy-it-now) may leave `closes_at` empty.
    requires_deadline: ClassVar[bool] = False

    #: Nobody may buy their own listing. Overridable, but no mode in this
    #: package does — see `LotteryMode` for why it matters most there.
    allows_seller_participation: ClassVar[bool] = False

    #: Whether one user may participate more than once (tickets: yes; a group
    #: buy slot: no).
    allows_repeat_participation: ClassVar[bool] = True

    #: The outcome is drawn rather than determined. Such a sale commits to a
    #: seed before it knows its entries, so the winner can be re-derived by
    #: anybody afterwards.
    draws_outcome: ClassVar[bool] = False

    # -- configuration ------------------------------------------------------

    @abc.abstractmethod
    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Normalise `config` or raise `SaleModeError` with an Italian message.

        Returns the *stored* config: validation and normalisation are the same
        pass, so what the database holds is always canonical and no reader has
        to defend against a missing key.
        """

    def initial_closes_at(self, config: dict[str, Any], opens_at: datetime) -> datetime | None:
        """When this sale should close, given its config."""
        days = config.get("duration_days")
        if days is None:
            return opens_at + timedelta(days=DEFAULT_SALE_DURATION_DAYS)
        return opens_at + timedelta(days=int(days))

    # -- lifecycle ----------------------------------------------------------

    def can_start(self, sale) -> Decision:
        """Last gate before the sale goes live and is announced."""
        if self.requires_deadline and sale.closes_at is None:
            return Decision.no("Questa modalità richiede una scadenza.")
        return Decision.yes()

    @abc.abstractmethod
    def on_participation(self, sale, user, amount: Amount, units: int) -> ParticipationResult:
        """Someone wants in. Accept, refuse, or accept-and-supersede.

        The mode owns the mechanic — minimum increment, ticket price, one slot
        per person — and calls `salemodes.holds` to reserve the money. It must
        not write to the ledger any other way.
        """

    def can_announce_settlement(self, sale) -> Decision:
        """May the seller *announce* an early settlement right now?

        Separate from `can_settle_early` on purpose. Announcing is what starts
        the notice window, so a mode whose early settlement requires a notice
        window would otherwise refuse the very announcement that satisfies it.
        Modes that need no window leave this refusing and let the seller call
        settlement directly.
        """
        return Decision.no("Questa vendita non prevede un annuncio di chiusura.")

    def can_settle_early(self, sale) -> Decision:
        """May this sale be closed before its deadline, right now?"""
        return Decision.no("Questa vendita non prevede una chiusura anticipata.")

    @abc.abstractmethod
    def settle(self, sale) -> SettlementResult:
        """Decide the outcome and capture the money that must change hands."""

    def refund(self, sale) -> RefundResult:
        """Give everything back.

        The default releases every active hold, which is right for every mode in
        this package: releasing a hold returns the exact minor units that were
        held, so a refund is exact by construction rather than by arithmetic.
        """
        from salemodes.holds import release_all

        return release_all(sale, reason="refund")

    # -- presentation -------------------------------------------------------

    def progress(self, sale) -> dict[str, Any]:
        """Numbers the template partial needs. Mode-specific, never assumed."""
        return {}

    def summary(self, sale) -> str:
        """One line describing the state of this sale, in Italian."""
        return self.label

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SaleMode {self.key}>"


# ---------------------------------------------------------------------------
# Config helpers — shared by the modes, kept here so their errors match
# ---------------------------------------------------------------------------


def require_int(config: dict[str, Any], key: str, *, minimum: int = 1,
                default: int | None = None, label: str = "") -> int:
    raw = config.get(key, default)
    if raw is None:
        raise SaleModeError(f"Manca il parametro «{label or key}».")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SaleModeError(f"«{label or key}» deve essere un numero intero.") from exc
    if value < minimum:
        raise SaleModeError(
            f"«{label or key}» deve essere almeno {minimum}, hai indicato {value}."
        )
    return value


def require_bool(config: dict[str, Any], key: str, *, default: bool = False) -> bool:
    raw = config.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "si", "sì", "yes", "on"}
    return bool(raw)

"""Outcome tables, RTP arithmetic and the four minigames.

The promise
-----------
Every game declares a **target RTP** and an **outcome table**, and the table is
checked against the target by exact rational arithmetic at import time. Not by
simulation — a Monte-Carlo check tells you a table is *probably* about right,
which is not the standard a published number deserves. The expected value of a
weighted table is a fraction of two integers, so it can be proven equal to the
declared figure and is.

`balance_table` exists so a table cannot silently drift. You declare the prizes
you want and the RTP you promised, and it solves for the weight of the losing
outcome that makes the two agree exactly, scaling the other weights by the
smallest integer factor needed. Hand-tuning a losing weight until a simulation
looks close is how a 94% game quietly becomes an 89% one.

Where randomness enters
-----------------------
Every draw comes from `games.fairness.Rng`, seeded by the committed proof. A
game's `derive()` is a pure function of that stream: same seed, same outcome,
forever. That is what makes a round verifiable months later, and it is why
presentation details (which reel stopped where, how many revolutions the wheel
turned) are drawn from the same stream rather than generated client-side — the
picture the player saw is part of what they can check.

The tables are authoritative, the pictures follow
-------------------------------------------------
The slot draws its *outcome* from the weighted table first, then renders a reel
combination consistent with it. The alternative — build reel strips, spin them
independently and read off whatever combination appears — makes the payout table
an emergent property of the strips, which is exactly the situation where a table
drifts from its declared RTP without anybody noticing. Regulated cabinets work
the way this one does, and for the same reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from math import lcm
from typing import Any

from core.contracts import DEFAULT_GAME_RTP_BASIS_POINTS
from core.exceptions import BiglieError
from games.constants import GameKey
from games.fairness import Rng

#: 10 000 basis points = 1× the stake returned.
ONE_X_BP = 10_000


class RtpMismatch(BiglieError):
    """An outcome table does not pay what its game declares.

    Raised at import time, so a table that drifts cannot reach production: the
    process refuses to boot rather than quietly running a game whose published
    RTP is a lie.
    """

    user_message = "Questo gioco è temporaneamente non disponibile."


class UnknownGame(BiglieError):
    """Asked for a game key that is not in the registry."""

    user_message = "Questo gioco non esiste."


# ---------------------------------------------------------------------------
# Outcome tables
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutcomeSlot:
    """One line of a payout table.

    `weight` is relative, not a probability: probabilities are only meaningful
    against the table's total, and keeping weights as integers is what lets the
    expected value be computed exactly.
    """

    key: str
    weight: int
    multiplier_bp: int
    label: str

    def __post_init__(self) -> None:
        if self.weight <= 0:
            raise RtpMismatch(f"Outcome {self.key!r} has non-positive weight {self.weight}.")
        if self.multiplier_bp < 0:
            raise RtpMismatch(f"Outcome {self.key!r} has a negative multiplier.")

    @property
    def is_win(self) -> bool:
        return self.multiplier_bp > 0

    @property
    def multiplier_display(self) -> str:
        """'2.5×' — for the paytable the player reads before staking."""
        value = Fraction(self.multiplier_bp, ONE_X_BP)
        return f"{float(value):g}×"


Table = tuple[OutcomeSlot, ...]


def expected_rtp(table: Table) -> Fraction:
    """The table's exact expected return, in basis points.

    A `Fraction`, deliberately. Returning a float here would reintroduce at the
    verification step precisely the imprecision the integer weights exist to
    avoid, and `Fraction(...) == 9400` is a test that means something.
    """
    total_weight = sum(slot.weight for slot in table)
    if total_weight <= 0:
        raise RtpMismatch("An outcome table needs at least one positive weight.")
    return Fraction(
        sum(slot.weight * slot.multiplier_bp for slot in table), total_weight
    )


def assert_table_rtp(table: Table, target_bp: int, *, name: str = "") -> None:
    """Prove a table pays exactly what it claims, or refuse to load it."""
    actual = expected_rtp(table)
    if actual != target_bp:
        raise RtpMismatch(
            f"Outcome table {name or '<unnamed>'} pays {float(actual):.4f} bp but "
            f"declares {target_bp} bp. Rebuild it with balance_table() rather than "
            "adjusting a weight until it looks close."
        )


def balance_table(
    paying: Table,
    target_bp: int = DEFAULT_GAME_RTP_BASIS_POINTS,
    *,
    loss_key: str = "niente",
    loss_label: str = "Niente",
) -> Table:
    """Append the losing outcome whose weight makes the table pay `target_bp`.

    Solves ``sum(w·m) / (W + x) = target`` for `x`. When `x` is not an integer
    the paying weights are scaled by its denominator — the smallest factor that
    makes every weight whole while leaving all the ratios untouched. The result
    hits the target *exactly*, not to within a tolerance.
    """
    if target_bp <= 0:
        raise RtpMismatch("A target RTP must be positive.")

    paid = sum(slot.weight * slot.multiplier_bp for slot in paying)
    paying_weight = sum(slot.weight for slot in paying)

    required_total = Fraction(paid, target_bp)
    if required_total <= paying_weight:
        raise RtpMismatch(
            f"The paying outcomes alone already return {float(Fraction(paid, paying_weight)):.0f} bp, "
            f"which is at or below the {target_bp} bp target. There is no losing "
            "weight that can fix this — the prizes are too small."
        )

    losing = required_total - paying_weight
    scale = losing.denominator

    table = tuple(
        OutcomeSlot(s.key, s.weight * scale, s.multiplier_bp, s.label) for s in paying
    ) + (OutcomeSlot(loss_key, losing.numerator, 0, loss_label),)

    assert_table_rtp(table, target_bp, name=loss_key)
    return table


def table_from_probabilities(
    rows: tuple[tuple[Fraction, str, int, str], ...],
) -> Table:
    """Build a table from exact probabilities `(p, key, multiplier_bp, label)`.

    Used by the ladder, whose distribution is derived from its own step odds
    rather than chosen. Probabilities are converted to integer weights over
    their common denominator, so nothing is rounded on the way in.
    """
    if sum(p for p, _, _, _ in rows) != 1:
        raise RtpMismatch("Probabilities must sum to exactly 1.")

    denominator = lcm(*(p.denominator for p, _, _, _ in rows))
    return tuple(
        OutcomeSlot(key, int(p * denominator), multiplier_bp, label)
        for p, key, multiplier_bp, label in rows
    )


# ---------------------------------------------------------------------------
# What a round produces
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    """The complete, verifiable result of one derivation.

    `detail` is the animation payload — see the module docstring of
    `games.services` for the shape the canvas frontend consumes. It contains
    only JSON-native types so it can be stored on the round and replayed.
    """

    game_key: str
    slot_key: str
    label: str
    multiplier_bp: int
    detail: dict[str, Any] = field(default_factory=dict)
    #: False only for the ladder, whose payout depends on a decision the player
    #: has not made yet.
    settled: bool = True

    def as_json(self) -> dict[str, Any]:
        return {
            "game_key": self.game_key,
            "slot_key": self.slot_key,
            "label": self.label,
            "multiplier_bp": self.multiplier_bp,
            "detail": self.detail,
            "settled": self.settled,
        }


def payout_minor(stake_minor: int, multiplier_bp: int) -> int:
    """Apply a multiplier to a stake, in integer minor units.

    Rounds **down**, and the rounding is in the house's favour by at most one
    scaglia per round. That is stated rather than hidden: rounding a payout up
    would let a player mint a scaglia a click, which is a real exploit at scale.
    """
    return stake_minor * multiplier_bp // ONE_X_BP


# ---------------------------------------------------------------------------
# Game definitions
# ---------------------------------------------------------------------------


class GameDefinition:
    """A minigame: a table, a target RTP and a derivation from the byte stream."""

    key: str = ""
    name: str = ""
    tagline: str = ""
    rtp_bp: int = DEFAULT_GAME_RTP_BASIS_POINTS
    #: The ladder is the only multi-step game; the flag drives whether
    #: `play_claim_gamble` settles immediately or hands back a live round.
    multi_step: bool = False

    def __init__(self) -> None:
        self.table = self.build_table()
        assert_table_rtp(self.table, self.rtp_bp, name=self.key)

    def build_table(self) -> Table:
        raise NotImplementedError

    def derive(self, rng: Rng) -> Outcome:
        """Turn the byte stream into an outcome. Pure, and the only entry point."""
        raise NotImplementedError

    # -- shared helpers -----------------------------------------------------

    def draw_slot(self, rng: Rng) -> OutcomeSlot:
        return self.table[rng.weighted_index([s.weight for s in self.table])]

    @property
    def house_edge_bp(self) -> int:
        return ONE_X_BP - self.rtp_bp

    @property
    def hit_rate(self) -> Fraction:
        """Share of rounds that pay anything. Shown next to the RTP, because a
        94% game that pays once in fifty feels nothing like a 94% game that pays
        every other round, and the player deserves both numbers."""
        total = sum(s.weight for s in self.table)
        return Fraction(sum(s.weight for s in self.table if s.is_win), total)

    @property
    def rtp_percent(self) -> float:
        """The declared RTP as a percentage, for the label on the game card."""
        return round(self.rtp_bp / 100, 2)

    @property
    def hit_rate_percent(self) -> float:
        return round(float(self.hit_rate) * 100, 2)

    def paytable(self) -> list[dict[str, Any]]:
        """The table, rendered for the page that shows the player their odds."""
        total = sum(s.weight for s in self.table)
        return [
            {
                "key": slot.key,
                "label": slot.label,
                "multiplier_bp": slot.multiplier_bp,
                "multiplier": slot.multiplier_display,
                "weight": slot.weight,
                "chance_percent": round(float(Fraction(slot.weight, total)) * 100, 4),
            }
            for slot in self.table
        ]


# -- slot ---------------------------------------------------------------------

#: Glyphs, matching the defaults in `web/static/js/games/slot.js`. The canvas
#: receives these strings verbatim and plants them on the payline.
SLOT_SYMBOLS: tuple[str, ...] = ("●", "▲", "◆", "✦", "★", "◍", "7")

SLOT_SYMBOL_NAMES: dict[str, str] = {
    "●": "sfera",
    "▲": "punta",
    "◆": "rombo",
    "✦": "scintilla",
    "★": "stella",
    "◍": "biglia",
    "7": "sette",
}

#: (symbol, weight, multiplier_bp) for three of a kind. Rarer pays more, which
#: is the only ordering a player will accept as fair without being told.
SLOT_TRIPLES: tuple[tuple[str, int, int], ...] = (
    ("●", 120, 30_000),
    ("▲", 80, 40_000),
    ("◆", 45, 60_000),
    ("✦", 25, 90_000),
    ("★", 12, 150_000),
    ("◍", 5, 300_000),
    ("7", 2, 600_000),
)

SLOT_PAIR_WEIGHT = 5_000
SLOT_PAIR_MULTIPLIER_BP = 15_000


class SlotMachine(GameDefinition):
    key = GameKey.SLOT
    name = "Slot"
    tagline = "Tre rulli. Due uguali pagano, tre uguali pagano molto."

    def build_table(self) -> Table:
        paying = tuple(
            OutcomeSlot(
                key=f"tris:{symbol}",
                weight=weight,
                multiplier_bp=multiplier,
                label=f"Tris di {SLOT_SYMBOL_NAMES[symbol]}",
            )
            for symbol, weight, multiplier in SLOT_TRIPLES
        ) + (
            OutcomeSlot("coppia", SLOT_PAIR_WEIGHT, SLOT_PAIR_MULTIPLIER_BP, "Coppia"),
        )
        return balance_table(paying, self.rtp_bp, loss_label="Nessuna combinazione")

    def derive(self, rng: Rng) -> Outcome:
        slot = self.draw_slot(rng)

        if slot.key.startswith("tris:"):
            symbol = slot.key.split(":", 1)[1]
            reels = [symbol, symbol, symbol]
        elif slot.key == "coppia":
            # Which symbol pairs up, where the odd reel sits, and what it shows.
            # All three drawn from the stream so the picture is verifiable too.
            matched = rng.choice(SLOT_SYMBOLS)
            odd_position = rng.random_below(3)
            others = [s for s in SLOT_SYMBOLS if s != matched]
            reels = [matched, matched, matched]
            reels[odd_position] = rng.choice(others)
        else:
            reels = rng.sample_distinct(SLOT_SYMBOLS, 3)

        return Outcome(
            game_key=self.key,
            slot_key=slot.key,
            label=slot.label,
            multiplier_bp=slot.multiplier_bp,
            detail={
                "reels": reels,
                "symbol_names": [SLOT_SYMBOL_NAMES[s] for s in reels],
            },
        )


# -- wheel --------------------------------------------------------------------

#: The wheel, wedge by wedge, clockwise from the pointer. Every wedge is one
#: entry with equal weight, so the arcs the player sees are exactly the odds —
#: `wheel.js` sizes each arc by weight and would happily draw a lie if we
#: collapsed the losing wedges into one fat slice.
WHEEL_WEDGES: tuple[int, ...] = (
    100_000, 0, 10_000, 0, 0,
    15_000, 0, 0, 20_000, 0,
    0, 15_000, 0, 0, 30_000,
    0, 0, 10_000, 0, 0,
    20_000, 0, 15_000, 0, 0,
)

WHEEL_WEDGE_LABELS: dict[int, str] = {
    0: "—",
    10_000: "1×",
    15_000: "1.5×",
    20_000: "2×",
    30_000: "3×",
    100_000: "10×",
}


class FortuneWheel(GameDefinition):
    key = GameKey.WHEEL
    name = "Ruota"
    tagline = "Venticinque spicchi. Uno paga dieci volte."

    def build_table(self) -> Table:
        # The wheel is the one game whose table is *given* rather than solved:
        # the wedges are the physical object, so the layout is the table and
        # `assert_table_rtp` in __init__ is what keeps it honest. Adding a prize
        # wedge without removing a blank breaks the boot, which is the point.
        counts: dict[int, int] = {}
        for multiplier in WHEEL_WEDGES:
            counts[multiplier] = counts.get(multiplier, 0) + 1

        return tuple(
            OutcomeSlot(
                key=f"wedge:{multiplier}",
                weight=count,
                multiplier_bp=multiplier,
                label=WHEEL_WEDGE_LABELS.get(multiplier, f"{multiplier / ONE_X_BP:g}×"),
            )
            for multiplier, count in sorted(counts.items())
        )

    def derive(self, rng: Rng) -> Outcome:
        # Drawn as a wedge index directly: on a wheel every wedge is equally
        # likely by construction, so a uniform index *is* the weighted draw.
        index = rng.random_below(len(WHEEL_WEDGES))
        multiplier = WHEEL_WEDGES[index]

        return Outcome(
            game_key=self.key,
            slot_key=f"wedge:{multiplier}",
            label=WHEEL_WEDGE_LABELS.get(multiplier, ""),
            multiplier_bp=multiplier,
            detail={
                "wedge_index": index,
                "wedges": wheel_wedges_payload(),
            },
        )


def wheel_wedges_payload() -> list[dict[str, Any]]:
    """The wedge list `FortuneWheel` in the frontend expects.

    Shape per wedge: ``{"label": str, "multiplier_bp": int, "weight": int}``.
    Equal weights, one entry per physical wedge.
    """
    return [
        {
            "label": WHEEL_WEDGE_LABELS.get(multiplier, f"{multiplier / ONE_X_BP:g}×"),
            "multiplier_bp": multiplier,
            "weight": 1,
        }
        for multiplier in WHEEL_WEDGES
    ]


# -- coinflip -----------------------------------------------------------------

class CoinFlip(GameDefinition):
    key = GameKey.COINFLIP
    name = "Testa o croce"
    tagline = "Un secondo, due esiti. Testa quasi raddoppia."

    # A genuinely fair coin — 50/50 — paying 1.88× instead of 2×.
    #
    # The alternative is a 2× payout on a 47% coin, which has the same RTP and
    # reads as a rigged coin the first time somebody counts. Putting the edge in
    # the payout keeps the one thing the player can check by eye honest.
    def build_table(self) -> Table:
        # 2 × rtp: at even odds the winning side must pay double the target for
        # the average to land on it. With rtp = 9400 this is 18800 bp = 1.88×.
        win_bp = 2 * self.rtp_bp
        return (
            OutcomeSlot("testa", 1, win_bp, f"Testa · {win_bp / ONE_X_BP:g}×"),
            OutcomeSlot("croce", 1, 0, "Croce · niente"),
        )

    def derive(self, rng: Rng) -> Outcome:
        slot = self.draw_slot(rng)
        return Outcome(
            game_key=self.key,
            slot_key=slot.key,
            label=slot.label,
            multiplier_bp=slot.multiplier_bp,
            detail={
                "face": slot.key,
                "won": slot.is_win,
                # Half-rotations, purely cosmetic — but drawn from the stream so
                # even the animation is part of what a sceptic can replay.
                "flips": rng.random_int(7, 14),
            },
        )


# -- ladder -------------------------------------------------------------------

#: Cash-out value at each rung, in basis points of the stake.
#:
#: The design constraint is stronger than "94% on average": *every* stopping
#: strategy returns exactly 94%. That falls out of setting each step's survival
#: chance to ``previous_rung / next_rung``, so climbing is EV-neutral and the
#: entire house edge is charged once, on the first step. A press-your-luck game
#: where cashing out early and pushing to the top have different expected values
#: is a game that punishes the player for choosing wrong, and the choice should
#: be about nerve, not about arithmetic they cannot do at the table.
LADDER_RUNGS: tuple[int, ...] = (
    12_000, 16_000, 23_000, 34_000, 52_000, 83_000, 140_000, 250_000
)


class Ladder(GameDefinition):
    key = GameKey.LADDER
    name = "Scala"
    tagline = "Sali finché hai coraggio. Ogni gradino vale di più e regge di meno."
    multi_step = True

    #: The rungs, exposed on the instance so `services` never has to import the
    #: module-level tuple to ask how tall the ladder is.
    rungs: tuple[int, ...] = LADDER_RUNGS

    # -- odds ---------------------------------------------------------------

    def step_chance(self, rung: int) -> Fraction:
        """Exact probability of surviving the climb onto `rung` (1-indexed).

        ``rtp / rung_1`` for the first step and ``rung_{n-1} / rung_n``
        afterwards, which is what makes every stopping point worth the same.
        """
        previous = self.rtp_bp if rung == 1 else LADDER_RUNGS[rung - 2]
        return Fraction(previous, LADDER_RUNGS[rung - 1])

    def build_table(self) -> Table:
        """The distribution over final outcomes for a player who never cashes out.

        The riskiest strategy is the one used to state the RTP, because it is the
        one where the table is a complete description of the game. That it comes
        out at exactly 9400 — the same as cashing out on rung one — is the
        property the design is built around.
        """
        rows: list[tuple[Fraction, str, int, str]] = []
        reached = Fraction(1)

        for index, _rung in enumerate(LADDER_RUNGS, start=1):
            chance = self.step_chance(index)
            rows.append(
                (
                    reached * (1 - chance),
                    f"caduta:{index}",
                    0,
                    f"Caduto al gradino {index}",
                )
            )
            reached *= chance

        rows.append((reached, "cima", LADDER_RUNGS[-1], "Arrivato in cima"))
        return table_from_probabilities(tuple(rows))

    # -- derivation ---------------------------------------------------------

    def derive(self, rng: Rng) -> Outcome:
        """Roll the whole climb up front.

        Every step is decided before the player takes the first one, which is
        strictly stronger than rolling as they go: the server cannot react to a
        player who is doing well, and the entire path is covered by a single
        commit-reveal the player can check in one operation. What the player is
        actually choosing is where to stop reading.
        """
        survived: list[bool] = []
        for rung in range(1, len(LADDER_RUNGS) + 1):
            chance = self.step_chance(rung)
            ok = rng.random_below(chance.denominator) < chance.numerator
            survived.append(ok)
            if not ok:
                break

        fell_at = None if all(survived) else len(survived)
        top = fell_at is None

        return Outcome(
            game_key=self.key,
            slot_key="cima" if top else f"caduta:{fell_at}",
            label="Arrivato in cima" if top else f"Caduto al gradino {fell_at}",
            multiplier_bp=LADDER_RUNGS[-1] if top else 0,
            settled=False,
            detail={
                "path": survived,
                "fell_at": fell_at,
                "rungs": list(LADDER_RUNGS),
                "revealed_rungs": 0,
                "cashed_out": False,
                "final_rung": None,
            },
        )

    # -- rules --------------------------------------------------------------

    def multiplier_at(self, rung: int) -> int:
        """What cashing out on `rung` pays. Rung 0 does not exist: the stake is
        committed the moment the round starts, so there is no free look."""
        if rung <= 0:
            return 0
        return LADDER_RUNGS[min(rung, len(LADDER_RUNGS)) - 1]

    def chance_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "rung": index,
                "multiplier_bp": value,
                "multiplier": f"{value / ONE_X_BP:g}×",
                "chance_percent": round(float(self.step_chance(index)) * 100, 2),
            }
            for index, value in enumerate(LADDER_RUNGS, start=1)
        ]


# -- duel ---------------------------------------------------------------------


class DuelDraw(GameDefinition):
    """The coin that decides a duel — not a game a player enters on their own.

    Its table is 50/50 at 2×, i.e. **100% RTP**, and that is deliberate: the
    house takes `DUEL_RAKE_BASIS_POINTS` off the pot, so building an edge into
    the contest as well would charge twice for one event and make the duel a
    strictly worse bet than the minigames it sits beside.
    """

    key = GameKey.DUEL
    name = "Sfida"
    tagline = "Due tiri, il più alto vince. Il banco prende solo il rake."
    #: The one game that does not use DEFAULT_GAME_RTP_BASIS_POINTS, and says so.
    rtp_bp = ONE_X_BP

    #: Faces per die. 1000 keeps ties rare enough to be a curiosity rather than
    #: a wait, while staying small enough to read on screen.
    DIE = 1_000

    def build_table(self) -> Table:
        return (
            OutcomeSlot("sfidante", 1, 2 * ONE_X_BP, "Vince lo sfidante"),
            OutcomeSlot("sfidato", 1, 0, "Vince lo sfidato"),
        )

    def derive(self, rng: Rng) -> Outcome:
        """Both sides roll; ties are re-rolled rather than broken by a rule.

        Breaking a tie in favour of either seat would make the seats unequal,
        and which seat you are in is not something a challenger and a defender
        agreed to at the same moment.
        """
        rolls: list[list[int]] = []
        while True:
            challenger = rng.random_int(1, self.DIE)
            defender = rng.random_int(1, self.DIE)
            rolls.append([challenger, defender])
            if challenger != defender:
                break

        challenger_wins = rolls[-1][0] > rolls[-1][1]
        return Outcome(
            game_key=self.key,
            slot_key="sfidante" if challenger_wins else "sfidato",
            label="Vince lo sfidante" if challenger_wins else "Vince lo sfidato",
            multiplier_bp=2 * ONE_X_BP if challenger_wins else 0,
            detail={
                "rolls": rolls,
                "challenger_roll": rolls[-1][0],
                "defender_roll": rolls[-1][1],
                "ties": len(rolls) - 1,
                "die": self.DIE,
                "challenger_wins": challenger_wins,
            },
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Instantiated at import. Every constructor runs `assert_table_rtp`, so an
#: unbalanced table is a boot failure rather than a slow leak in the accounts.
REGISTRY: dict[str, GameDefinition] = {
    definition.key: definition
    for definition in (SlotMachine(), FortuneWheel(), Ladder(), CoinFlip(), DuelDraw())
}

#: The games a player may choose for a claim gamble, in the order they are shown.
PLAYABLE: tuple[str, ...] = (GameKey.SLOT, GameKey.WHEEL, GameKey.LADDER, GameKey.COINFLIP)


def get_game(game_key: str) -> GameDefinition:
    try:
        return REGISTRY[str(game_key)]
    except KeyError:
        raise UnknownGame(f"No game registered under {game_key!r}.") from None


def playable_games() -> list[GameDefinition]:
    return [REGISTRY[key] for key in PLAYABLE]


def derive_for_verification(game_key: str) -> Callable[[Rng], dict[str, Any]]:
    """A derivation function shaped for `games.fairness.verify`.

    Returns the outcome as a plain dict so the verifier can compare it against
    what was stored without knowing anything about game classes.
    """
    definition = get_game(game_key)

    def derive(rng: Rng) -> dict[str, Any]:
        outcome = definition.derive(rng)
        return {
            "game_key": outcome.game_key,
            "slot_key": outcome.slot_key,
            "multiplier_bp": outcome.multiplier_bp,
            "detail": outcome.detail,
        }

    return derive

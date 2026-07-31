"""Value objects for everything the platform can move.

Two rules, and every bug this module exists to prevent comes from breaking one
of them:

1. **Amounts are integers in minor units.** Never floats, never Decimal at the
   boundary. A euro is 100 cents; a biglia is 100 scaglie. Rounding is an
   explicit, auditable operation (see `split_percentage`), not a side effect of
   how the CPU stores 0.1.

2. **Assets never mix implicitly.** Adding euros to biglie raises. Converting
   between them is a transaction with a rate, not an arithmetic operator.

On the scaglia: it is the *minor unit* of a biglia, not a fourth kind of biglia.
The dashboard tells the user their scaglie "fuse" into a whole biglia at 100, but
that is presentation — the ledger only ever sees integers.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from core.exceptions import AssetMismatch, InvalidAmount


class Asset(enum.StrEnum):
    """What kind of value an amount denominates.

    The three biglia flavours are not cosmetic — they carry different rights,
    and keeping them as separate assets means the ledger itself enforces those
    rights instead of trusting every call site to remember them.
    """

    EUR = "EUR"

    #: Bought with real money. The only biglia that may leave as euros again.
    BIGLIA_PIENA = "BIGLIA_PIENA"

    #: Produced by the platform: game winnings, lottery prizes, the passive
    #: drip. Spendable and wagerable, never withdrawable — this is the line
    #: that keeps "money in" separate from "money the house created".
    BIGLIA_VINTA = "BIGLIA_VINTA"

    #: Received from another user. Transferable but rate-limited and logged,
    #: because unrestricted peer transfer of a withdrawable asset is a money
    #: laundering pipe.
    BIGLIA_DONO = "BIGLIA_DONO"

    @property
    def minor_units(self) -> int:
        return 100  # cents per euro, scaglie per biglia

    @property
    def is_biglia(self) -> bool:
        return self is not Asset.EUR

    @property
    def is_withdrawable(self) -> bool:
        return self in (Asset.EUR, Asset.BIGLIA_PIENA)

    @property
    def is_wagerable(self) -> bool:
        return self.is_biglia

    @property
    def is_transferable(self) -> bool:
        """Whether a user may gift this asset to another user."""
        return self in (Asset.BIGLIA_PIENA, Asset.BIGLIA_DONO)

    @property
    def label(self) -> str:
        return {
            Asset.EUR: "euro",
            Asset.BIGLIA_PIENA: "biglie piene",
            Asset.BIGLIA_VINTA: "biglie vinte",
            Asset.BIGLIA_DONO: "biglie dono",
        }[self]


#: Assets a user can spend inside the marketplace, in the order they should be
#: consumed. Spending the least valuable first is deliberate: it leaves the
#: user's withdrawable balance intact for as long as possible, which is both
#: fairer and reduces our redemption liability.
SPEND_PRIORITY = (Asset.BIGLIA_VINTA, Asset.BIGLIA_DONO, Asset.BIGLIA_PIENA)


@dataclass(frozen=True, slots=True, order=False)
class Amount:
    """An integer quantity of one asset, in minor units."""

    minor: int
    asset: Asset

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise InvalidAmount(
                f"Amounts are integers in minor units, got {type(self.minor).__name__}. "
                "Use Amount.from_major() if you are starting from a human figure."
            )

    # -- constructors -------------------------------------------------------

    @classmethod
    def zero(cls, asset: Asset) -> Amount:
        return cls(0, asset)

    @classmethod
    def from_major(cls, major: str | int | Decimal, asset: Asset) -> Amount:
        """Build from a human-facing figure ("12.34" euro, "3.50" biglie).

        Accepts str/int/Decimal only. Passing a float is rejected: 0.1 is not
        0.1, and this is the exact boundary where that stops being pedantry.
        """
        if isinstance(major, float):
            raise InvalidAmount(
                "Refusing to build an Amount from a float. Pass a string or Decimal "
                "so the value you meant is the value that gets stored."
            )
        quantum = Decimal(1).scaleb(-2)
        value = (Decimal(major) * asset.minor_units).quantize(quantum, rounding=ROUND_DOWN)
        return cls(int(value), asset)

    # -- arithmetic ---------------------------------------------------------

    def _check(self, other: Amount) -> None:
        if not isinstance(other, Amount):
            raise AssetMismatch(f"Cannot combine Amount with {type(other).__name__}.")
        if self.asset is not other.asset:
            raise AssetMismatch(
                f"Refusing to mix {self.asset} and {other.asset}. Assets convert "
                "through an explicit exchange, never through arithmetic."
            )

    def __add__(self, other: Amount) -> Amount:
        self._check(other)
        return Amount(self.minor + other.minor, self.asset)

    def __sub__(self, other: Amount) -> Amount:
        self._check(other)
        return Amount(self.minor - other.minor, self.asset)

    def __neg__(self) -> Amount:
        return Amount(-self.minor, self.asset)

    def __mul__(self, factor: int) -> Amount:
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise InvalidAmount(
                "Multiply an Amount by an integer only. To take a percentage use "
                "split_percentage(), which is explicit about where the remainder goes."
            )
        return Amount(self.minor * factor, self.asset)

    __rmul__ = __mul__

    def __lt__(self, other: Amount) -> bool:
        self._check(other)
        return self.minor < other.minor

    def __le__(self, other: Amount) -> bool:
        self._check(other)
        return self.minor <= other.minor

    def __gt__(self, other: Amount) -> bool:
        self._check(other)
        return self.minor > other.minor

    def __ge__(self, other: Amount) -> bool:
        self._check(other)
        return self.minor >= other.minor

    def __bool__(self) -> bool:
        return self.minor != 0

    # -- properties ---------------------------------------------------------

    @property
    def is_positive(self) -> bool:
        return self.minor > 0

    @property
    def is_negative(self) -> bool:
        return self.minor < 0

    @property
    def major(self) -> Decimal:
        """The human-facing figure. For display and reports — never for maths."""
        return (Decimal(self.minor) / self.asset.minor_units).quantize(Decimal("0.01"))

    @property
    def whole(self) -> int:
        """Whole units, discarding the remainder (37 whole biglie of 3742 scaglie)."""
        return abs(self.minor) // self.asset.minor_units * (1 if self.minor >= 0 else -1)

    @property
    def fraction(self) -> int:
        """The leftover minor units — the "scaglie toward the next biglia" counter."""
        return abs(self.minor) % self.asset.minor_units

    def __str__(self) -> str:
        symbol = "€" if self.asset is Asset.EUR else "◍"
        return f"{symbol}{self.major}"

    def __repr__(self) -> str:
        return f"Amount({self.minor}, {self.asset.name})"


def split_percentage(total: Amount, basis_points: int) -> tuple[Amount, Amount]:
    """Split `total` into (cut, remainder) at `basis_points` (1% = 100 bps).

    The cut rounds *down* and the remainder absorbs the leftover minor unit, so
    the two halves always add back to exactly `total`. That property is what
    makes the charity split and the house rake safe to compose: no matter how
    many times value is divided on its way through a transaction, nothing is
    conjured and nothing evaporates.
    """
    if not 0 <= basis_points <= 10_000:
        raise InvalidAmount(f"basis_points must be within 0..10000, got {basis_points}.")
    if total.is_negative:
        raise InvalidAmount("Refusing to split a negative amount; split the positive leg.")

    cut = Amount(total.minor * basis_points // 10_000, total.asset)
    return cut, total - cut


def allocate(total: Amount, weights: list[int]) -> list[Amount]:
    """Distribute `total` across `weights`, giving remainder minor units to the
    largest weights first.

    Used for lottery prize tiers and multi-beneficiary charity payouts. The sum
    of the result is always exactly `total` — verified by property test.
    """
    if not weights or any(w < 0 for w in weights) or sum(weights) == 0:
        raise InvalidAmount("allocate() needs at least one positive weight.")

    total_weight = sum(weights)
    shares = [Amount(total.minor * w // total_weight, total.asset) for w in weights]
    remainder = total.minor - sum(s.minor for s in shares)

    # Hand the odd minor units to the biggest weights, largest first, so the
    # rounding bias is at least deterministic and explicable.
    for index in sorted(range(len(weights)), key=lambda i: (-weights[i], i))[:remainder]:
        shares[index] = shares[index] + Amount(1, total.asset)

    return shares

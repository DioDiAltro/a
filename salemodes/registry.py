"""The sale-mode registry.

Public API of this context alongside `services`: `marketplace` imports this to
build the "come vuoi venderlo?" chooser and to render each mode's partial. It
never imports a mode class directly, which is what keeps the order engine from
growing a branch per mechanic.

Registration is a decorator on the class, and the modules under
`salemodes.modes` are imported once from `SalemodesConfig.ready()`. A mode
living in another app — or in a test — registers exactly the same way.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from core.exceptions import SaleModeError
from salemodes.base import SaleMode

logger = logging.getLogger(__name__)

#: key -> instance. Modes are stateless singletons; see `SaleMode`.
_MODES: dict[str, SaleMode] = {}


def register(cls: type[SaleMode]) -> type[SaleMode]:
    """Class decorator: make a mode available to the whole platform.

    Raises on a duplicate key rather than overwriting. Two modes answering to
    "lottery" would make which one runs a function of import order, and import
    order is not a design decision anybody wants to be making.
    """
    key = getattr(cls, "key", "")
    if not key:
        raise SaleModeError(f"{cls.__name__} must declare a non-empty `key`.")
    if key in _MODES and type(_MODES[key]) is not cls:
        raise SaleModeError(
            f"Sale mode key {key!r} is already registered to "
            f"{type(_MODES[key]).__name__}. Keys must be unique."
        )

    _MODES[key] = cls()
    logger.debug("Registered sale mode %s -> %s", key, cls.__name__)
    return cls


def unregister(key: str) -> None:
    """Drop a mode. For tests and for hot-disabling a broken mechanic.

    Deliberately does not touch sales already using the key: those rows keep
    their mode string and will raise a clear `SaleModeError` on the next
    lifecycle call, which is a better failure than silently settling under
    different rules.
    """
    _MODES.pop(key, None)


def get(key: str) -> SaleMode:
    """The mode registered under `key`, or a domain error naming what exists."""
    try:
        return _MODES[key]
    except KeyError:
        raise SaleModeError(
            f"Modalità di vendita sconosciuta: {key!r}. Disponibili: "
            f"{', '.join(sorted(_MODES)) or 'nessuna'}."
        ) from None


def has(key: str) -> bool:
    return key in _MODES


def all_modes() -> tuple[SaleMode, ...]:
    """Every registered mode, ordered by key so the UI is stable."""
    return tuple(_MODES[key] for key in sorted(_MODES))


def choices() -> list[tuple[str, str]]:
    """`(key, label)` pairs for a form field."""
    return [(mode.key, mode.label) for mode in all_modes()]


def __iter__() -> Iterator[SaleMode]:  # pragma: no cover - convenience
    return iter(all_modes())

"""Sale mechanics.

Importing this package registers every mode. `SalemodesConfig.ready()` does it
once at startup; nothing else should need to.

Adding a mechanic is: a module here, a `@register` on the class, and a template
partial. No change to `marketplace`, no migration, no branch in the order
engine — that is the whole point of the registry, and
`salemodes/tests/test_plugin_seam.py` proves it by driving a mode that lives
only inside a test.
"""

from salemodes.modes import (  # noqa: F401  (imported for the side effect)
    auction,
    challenge,
    fixed,
    groupbuy,
    lottery,
    mysterybox,
)

__all__ = ["auction", "challenge", "fixed", "groupbuy", "lottery", "mysterybox"]

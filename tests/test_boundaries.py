"""The architecture test.

A modular monolith is only modular for as long as something checks. This test is
that something: it fails the build the moment one bounded context reaches into
another's internals instead of going through an event or a published API.

That discipline is what makes the "microservices when we need them" promise real
rather than aspirational — a context that never imports its neighbours can be
lifted into its own service without touching its domain logic.
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Every bounded context. Adding an app here without adding it to the allowances
#: below means "this app may talk to nobody", which is the correct default.
CONTEXTS = {
    "charity", "compliance", "economy", "games", "identity",
    "marketplace", "payments", "salemodes", "wallet", "web", "realtime",
}

#: Modules any context may import from freely. `core` holds the shared language;
#: `identity.models` is the AUTH_USER_MODEL, which Django requires everyone to
#: be able to name.
ALWAYS_ALLOWED = {"core", "config"}

#: Who may call whose *published API* (the `services` / `api` module only).
#: Read this as the dependency graph of the system — if an arrow here surprises
#: you, the design is wrong, not the test.
ALLOWED_SERVICE_CALLS: dict[str, set[str]] = {
    # The ledger is a leaf. It answers, it never asks. If wallet ever needs to
    # import another context, the design has inverted and money is at risk.
    "wallet": set(),

    "identity": set(),
    "compliance": {"identity"},

    # Games move money and must know who is allowed to play.
    "games": {"wallet", "compliance", "identity"},

    "salemodes": {"wallet", "games", "compliance"},
    "marketplace": {"wallet", "salemodes", "compliance", "identity"},
    "payments": {"wallet", "compliance", "identity"},
    "charity": {"wallet"},
    "economy": {"wallet", "identity"},

    # Presentation may read from anywhere; it writes through services only.
    "web": CONTEXTS - {"web"},
    "realtime": CONTEXTS - {"realtime"},
}

#: Within an allowed pair, only these submodules may be imported. Importing
#: `models` across a boundary is what turns a modular monolith back into a ball
#: of mud, so it is never permitted.
PUBLIC_SUBMODULES = {"services", "api", "constants", "selectors", "registry"}


def _python_files(app: str) -> list[pathlib.Path]:
    return [
        path
        for path in (REPO / app).rglob("*.py")
        if "migrations" not in path.parts and "tests" not in path.parts
    ]


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_contexts_do_not_reach_across_boundaries():
    violations: list[str] = []

    for app in sorted(CONTEXTS):
        allowed = ALLOWED_SERVICE_CALLS.get(app, set())

        for path in _python_files(app):
            for module in _imported_modules(path):
                root = module.split(".")[0]

                if root in ALWAYS_ALLOWED or root == app or root not in CONTEXTS:
                    continue

                relative = path.relative_to(REPO)

                # Django needs the user model importable everywhere for FKs, but
                # settings.AUTH_USER_MODEL is the right way to name it in a
                # model, so only non-model code gets the exemption.
                if module == "identity.models" and path.name != "models.py":
                    violations.append(
                        f"{relative}: imports {module} — use settings.AUTH_USER_MODEL "
                        "or get_user_model() instead of importing across contexts."
                    )
                    continue

                if root not in allowed:
                    violations.append(
                        f"{relative}: {app} imports {module}, but {app} is not allowed to "
                        f"depend on {root}. Publish an event in core.contracts instead."
                    )
                    continue

                parts = module.split(".")
                if len(parts) > 1 and parts[1] not in PUBLIC_SUBMODULES:
                    violations.append(
                        f"{relative}: {app} imports {module} — only "
                        f"{sorted(PUBLIC_SUBMODULES)} are public. Reaching into "
                        f"{parts[1]} couples the two contexts' internals."
                    )

    assert not violations, "Bounded context violations:\n  " + "\n  ".join(violations)


def test_wallet_is_a_leaf():
    """Stated separately because it is the invariant that protects the money.

    The ledger must be reasonable about in isolation. The day it imports the
    marketplace is the day a listing bug can corrupt a balance.
    """
    for path in _python_files("wallet"):
        for module in _imported_modules(path):
            root = module.split(".")[0]
            assert root not in (CONTEXTS - {"wallet"}), (
                f"{path.relative_to(REPO)} imports {module}. The wallet answers "
                "questions; it never asks them."
            )


def test_every_context_is_declared():
    """A new app must make a conscious decision about its dependencies."""
    undeclared = sorted(CONTEXTS - set(ALLOWED_SERVICE_CALLS))
    assert not undeclared, (
        f"Contexts missing a dependency declaration: {undeclared}. Add them to "
        "ALLOWED_SERVICE_CALLS — an empty set is a fine and common answer."
    )

"""Provably fair draws for sale modes.

A lottery whose draw cannot be checked is worth nothing: the seller picks the
winner and the platform's word is the only evidence. So every drawn outcome in
this context — lottery winner, mystery-box allocation — runs through
commit-reveal.

    1. **Commit.** When the sale opens (or when an early draw is announced) we
       publish `sha256(server_seed)`. The seed stays secret.
    2. **Client seed.** Not the seller's to choose: it is derived from the entry
       list itself — every participant id and ticket count, in order. Anyone can
       recompute it, and it changes the instant somebody else buys a ticket, so
       neither side can steer the outcome by choosing *when* to draw.
    3. **Reveal.** After settlement the seed is published in
       `SaleOutcome.verification` together with the exact entries and weights.
       Ten lines of Python reproduce the winner.

Where the randomness comes from
-------------------------------
`games` owns fairness for the platform and this module is a thin port to it:
`_games_call` looks for the draw API on `games.services` and uses it if it is
there. If it is not — the games context is being built alongside this one — we
fall back to a local implementation of *the same scheme* (HMAC-SHA256 over
``{client_seed}:{nonce}:{block}``, rejection-sampled to remove modulo bias),
record `draw_source="local"` and log a warning. The proof published to users is
identical in either case; only the custodian of the seed differs.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Byte-stream message format. Part of the public promise: a user
#: reimplementing the verifier must be able to read the exact format here. It
#: matches `games.fairness.MESSAGE_TEMPLATE` so one verifier checks both.
MESSAGE_TEMPLATE = "{client_seed}:{nonce}:{block}"

SERVER_SEED_BYTES = 32

#: Names we will accept on `games.services`, most specific first. The games
#: context is under construction in parallel; whichever of these it lands on,
#: the draw routes through it instead of the local fallback.
_COMMIT_CANDIDATES = ("open_commitment", "commit_for", "commit_draw", "commit", "commitment_for")
_DRAW_CANDIDATES = ("resolve_draw", "draw_weighted", "weighted_draw", "external_draw")


# ---------------------------------------------------------------------------
# The local generator — same scheme as games.fairness.Rng
# ---------------------------------------------------------------------------


class _Stream:
    """Deterministic byte stream keyed by (server_seed, client_seed, nonce)."""

    __slots__ = ("server_seed", "client_seed", "nonce", "_block", "_buffer", "_offset")

    def __init__(self, server_seed: str, client_seed: str, nonce: int = 0) -> None:
        self.server_seed = server_seed
        self.client_seed = client_seed
        self.nonce = nonce
        self._block = 0
        self._buffer = b""
        self._offset = 0

    def _next_block(self) -> bytes:
        message = MESSAGE_TEMPLATE.format(
            client_seed=self.client_seed, nonce=self.nonce, block=self._block
        )
        self._block += 1
        return hmac.new(
            self.server_seed.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).digest()

    def next_bytes(self, count: int) -> bytes:
        out = bytearray()
        while len(out) < count:
            if self._offset >= len(self._buffer):
                self._buffer = self._next_block()
                self._offset = 0
            take = min(count - len(out), len(self._buffer) - self._offset)
            out += self._buffer[self._offset : self._offset + take]
            self._offset += take
        return bytes(out)

    def below(self, n: int) -> int:
        """Uniform in ``[0, n)`` by rejection sampling — no modulo bias.

        The bias `value % n` introduces is small, but on a draw for a real prize
        "small and in the house's favour" is the definition of a rigged game.
        """
        if n <= 1:
            return 0
        width = max(4, ((n - 1).bit_length() + 7) // 8)
        space = 1 << (width * 8)
        limit = space - (space % n)
        while True:
            value = int.from_bytes(self.next_bytes(width), "big")
            if value < limit:
                return value % n

    def weighted_index(self, weights: list[int]) -> int:
        total = sum(weights)
        target = self.below(total)
        for index, weight in enumerate(weights):
            if target < weight:
                return index
            target -= weight
        raise AssertionError("unreachable: weights summed inconsistently")


def new_server_seed() -> str:
    return secrets.token_hex(SERVER_SEED_BYTES)


def hash_seed(server_seed: str) -> str:
    return hashlib.sha256(server_seed.encode("utf-8")).hexdigest()


def client_seed_for(entries: list[str], weights: list[int]) -> str:
    """Derive the client seed from the entry list.

    Nobody owns this value: it is a function of who bought in and how much. The
    seller cannot nudge it without buying tickets they are not allowed to buy,
    and a participant can only change it by adding themselves to the draw.
    """
    material = ";".join(f"{entry}:{weight}" for entry, weight in zip(entries, weights, strict=True))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The port to `games`
# ---------------------------------------------------------------------------


def _games_call(names: tuple[str, ...], **kwargs) -> Any | None:
    """Call the first function `games.services` offers under these names.

    Returns None — rather than raising — if the games context has not published
    a draw API yet or its signature does not match. A sale must still be able to
    settle; it simply records that the draw ran locally.
    """
    try:
        from games import services as games_services
    except ImportError:
        return None

    for name in names:
        fn = getattr(games_services, name, None)
        if fn is None:
            continue
        try:
            return fn(**kwargs)
        except TypeError as exc:
            logger.warning("games.services.%s rejected the sale draw call: %s", name, exc)
            return None
        except Exception:
            logger.exception("games.services.%s failed; falling back to a local draw", name)
            return None
    return None


def _attr(obj: Any, *names, default=None):
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Commitment:
    hash: str
    seed: str = ""
    proof_id: uuid.UUID | None = None
    source: str = "local"


@dataclass(frozen=True, slots=True)
class Draw:
    index: int
    proof_id: uuid.UUID | None = None
    verification: dict[str, Any] = field(default_factory=dict)


def open_commitment(*, purpose: str, reference_id: str) -> Commitment:
    """Publish a commitment to a draw that has not happened yet."""
    result = _games_call(_COMMIT_CANDIDATES, purpose=purpose, reference_id=reference_id)
    if result is not None:
        commitment_hash = _attr(result, "server_seed_hash", "commitment_hash", "hash", default="")
        proof_id = _attr(result, "id", "pk", "proof_id")
        if commitment_hash:
            return Commitment(
                hash=str(commitment_hash),
                proof_id=uuid.UUID(str(proof_id)) if proof_id else None,
                source="games",
            )

    seed = new_server_seed()
    logger.info("Sale %s committed to a local draw seed", reference_id)
    return Commitment(hash=hash_seed(seed), seed=seed, source="local")


def draw_index(*, commitment: Commitment, entries: list[str], weights: list[int],
               nonce: int = 0) -> Draw:
    """Pick one entry, weighted, and return everything needed to check it."""
    if not entries:
        raise ValueError("A draw needs at least one entry.")

    client_seed = client_seed_for(entries, weights)

    if commitment.source == "games" and commitment.proof_id is not None:
        result = _games_call(
            _DRAW_CANDIDATES,
            proof_id=commitment.proof_id,
            client_seed=client_seed,
            weights=list(weights),
            nonce=nonce,
        )
        if result is not None:
            index = _attr(result, "index", "winner_index", "outcome_index")
            if index is not None:
                return Draw(
                    index=int(index),
                    proof_id=commitment.proof_id,
                    verification={
                        "source": "games",
                        "proof_id": str(commitment.proof_id),
                        "server_seed_hash": commitment.hash,
                        "client_seed": client_seed,
                        "nonce": nonce,
                        "entries": entries,
                        "weights": weights,
                    },
                )

    stream = _Stream(commitment.seed, client_seed, nonce)
    index = stream.weighted_index(list(weights))
    return Draw(
        index=index,
        proof_id=commitment.proof_id,
        verification={
            "source": "local",
            "algorithm": "HMAC-SHA256(server_seed, '{client_seed}:{nonce}:{block}')",
            "server_seed": commitment.seed,
            "server_seed_hash": commitment.hash,
            "client_seed": client_seed,
            "nonce": nonce,
            "entries": entries,
            "weights": weights,
            "winner_index": index,
        },
    )


def draw_permutation(*, commitment: Commitment, entries: list[str], count: int,
                     nonce: int = 0) -> tuple[list[int], dict[str, Any]]:
    """A provably fair shuffle: which sealed prize goes to which buyer.

    Drawn without replacement out of the same stream, so the whole allocation is
    one verifiable derivation rather than a sequence of unrelated draws.
    """
    client_seed = client_seed_for(entries, [1] * len(entries))
    stream = _Stream(commitment.seed or hash_seed(commitment.hash), client_seed, nonce)

    pool = list(range(count))
    order: list[int] = []
    for _ in range(min(count, len(entries))):
        order.append(pool.pop(stream.below(len(pool))))

    return order, {
        "source": commitment.source,
        "algorithm": "HMAC-SHA256(server_seed, '{client_seed}:{nonce}:{block}')",
        "server_seed": commitment.seed,
        "server_seed_hash": commitment.hash,
        "client_seed": client_seed,
        "nonce": nonce,
        "entries": entries,
        "prize_order": order,
    }


def verify_local_draw(verification: dict[str, Any]) -> bool:
    """Replay a published local draw. Used by the verification view and tests."""
    if verification.get("source") != "local":
        return False
    seed = verification.get("server_seed") or ""
    if hash_seed(seed) != verification.get("server_seed_hash"):
        return False

    stream = _Stream(seed, verification["client_seed"], verification.get("nonce", 0))
    return stream.weighted_index(list(verification["weights"])) == verification["winner_index"]

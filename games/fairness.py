"""Provably fair randomness — the foundation everything else in `games` sits on.

A gambling surface where the player cannot check the result is not a game, it is
a request to trust us. This module makes that trust unnecessary.

The scheme
----------
1. **Commit.** Before a round exists, the server draws a 256-bit `server_seed`
   and publishes `sha256(server_seed)`. The seed itself stays secret.
2. **Choose.** The player supplies (or changes) their own `client_seed`. They do
   this *after* seeing the commitment, which is what makes the commitment
   binding: the server cannot pick a seed that suits the client seed, because it
   was already committed to one.
3. **Play.** The outcome is derived deterministically from
   ``HMAC-SHA256(server_seed, "{client_seed}:{nonce}:{block}")``, expanded into
   an unbounded byte stream by incrementing ``block``.
4. **Reveal.** After settlement the server publishes `server_seed`. Anyone can
   now hash it, compare against the commitment published in step 1, replay the
   HMAC and re-derive the exact outcome.

Neither side can steer the result: the server is bound by the hash it published
before it knew the client seed, and the player cannot compute anything from a
hash it cannot invert.

No modulo bias
--------------
`Rng.random_below` uses **rejection sampling**. The naive `word % n` is biased
toward small values whenever `n` does not divide the word space, and on a money
game that bias is a defect with a monetary value — it shifts the RTP away from
the number we published. We discard any word landing in the short tail and draw
again, which costs a few extra bytes and buys exact uniformity.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.exceptions import FairnessViolation, InvalidAmount
from core.models import BaseModel
from games.constants import CLIENT_SEED_MAX_LENGTH, SERVER_SEED_BYTES

#: The HMAC message. Kept as a module constant because it is part of the public
#: promise: a user reimplementing the verifier in ten lines of Python must be
#: able to read the exact format here.
MESSAGE_TEMPLATE = "{client_seed}:{nonce}:{block}"


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


class Rng:
    """A deterministic, uniform byte stream keyed by (server, client, nonce).

    Instances are single-use and stateful: every draw advances the cursor, so
    two draws never see the same bytes. Re-creating an `Rng` with the same three
    inputs replays the identical stream, which is exactly how verification
    works.
    """

    __slots__ = ("server_seed", "client_seed", "nonce", "_block", "_buffer", "_offset", "_drawn")

    def __init__(self, server_seed: str, client_seed: str, nonce: int) -> None:
        if not server_seed:
            raise FairnessViolation("Cannot derive an outcome without a server seed.")
        self.server_seed = server_seed
        self.client_seed = client_seed
        self.nonce = int(nonce)
        self._block = 0
        self._buffer = b""
        self._offset = 0
        self._drawn = 0

    # -- stream -------------------------------------------------------------

    def _next_block(self) -> bytes:
        message = MESSAGE_TEMPLATE.format(
            client_seed=self.client_seed, nonce=self.nonce, block=self._block
        )
        self._block += 1
        return hmac.new(
            self.server_seed.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).digest()

    def next_bytes(self, count: int) -> bytes:
        """Take `count` bytes off the stream."""
        if count <= 0:
            raise InvalidAmount("next_bytes needs a positive count.")
        out = bytearray()
        while len(out) < count:
            if self._offset >= len(self._buffer):
                self._buffer = self._next_block()
                self._offset = 0
            take = min(count - len(out), len(self._buffer) - self._offset)
            out += self._buffer[self._offset : self._offset + take]
            self._offset += take
        self._drawn += count
        return bytes(out)

    # -- uniform integers ---------------------------------------------------

    def random_below(self, n: int) -> int:
        """A uniform integer in ``[0, n)``, with no modulo bias.

        Draws whole machine words; any word in the truncated tail above the
        largest exact multiple of `n` is rejected and redrawn. The loop
        terminates with probability 1 and, because we always draw at least four
        bytes, the expected number of rejections is under one in 2^31 for every
        `n` this codebase uses.
        """
        if n <= 0:
            raise InvalidAmount(f"random_below needs a positive bound, got {n}.")
        if n == 1:
            return 0

        width = max(4, ((n - 1).bit_length() + 7) // 8)
        space = 1 << (width * 8)
        limit = space - (space % n)  # largest exact multiple of n

        while True:
            value = int.from_bytes(self.next_bytes(width), "big")
            if value < limit:
                return value % n

    def random_int(self, low: int, high: int) -> int:
        """A uniform integer in ``[low, high]``, both ends inclusive."""
        if high < low:
            raise InvalidAmount(f"random_int needs low <= high, got ({low}, {high}).")
        return low + self.random_below(high - low + 1)

    def weighted_index(self, weights: list[int] | tuple[int, ...]) -> int:
        """Pick an index with probability proportional to its weight.

        Built on `random_below`, so it inherits the absence of bias. This is the
        single primitive every outcome table is resolved through.
        """
        total = sum(weights)
        if total <= 0:
            raise InvalidAmount("A weighted draw needs at least one positive weight.")
        target = self.random_below(total)
        for index, weight in enumerate(weights):
            if target < weight:
                return index
            target -= weight
        raise AssertionError("unreachable: weights summed inconsistently")

    def choice(self, options):
        return options[self.random_below(len(options))]

    def sample_distinct(self, options, count: int) -> list:
        """`count` distinct members of `options`, drawn without replacement."""
        pool = list(options)
        if count > len(pool):
            raise InvalidAmount("Cannot draw more distinct items than exist.")
        drawn = []
        for _ in range(count):
            drawn.append(pool.pop(self.random_below(len(pool))))
        return drawn

    @property
    def bytes_drawn(self) -> int:
        """How much of the stream was consumed. Handy in verification output."""
        return self._drawn


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


def new_server_seed() -> str:
    return secrets.token_hex(SERVER_SEED_BYTES)


def hash_seed(server_seed: str) -> str:
    return hashlib.sha256(server_seed.encode("utf-8")).hexdigest()


def default_client_seed() -> str:
    """A starting client seed for a user who has not chosen one.

    Randomly generated rather than left empty so that a user who never touches
    the setting still gets a distinct stream — and so the verify page always has
    something concrete to show.
    """
    return secrets.token_hex(8)


def normalise_client_seed(raw: str | None) -> str:
    """Clean a user-supplied client seed.

    Deliberately permissive: it is *their* entropy contribution and the whole
    point is that they control it. We only trim whitespace, cap the length and
    fall back to a generated seed when they give us nothing.
    """
    seed = (raw or "").strip()
    if not seed:
        return default_client_seed()
    return seed[:CLIENT_SEED_MAX_LENGTH]


# ---------------------------------------------------------------------------
# The proof
# ---------------------------------------------------------------------------


class FairnessProofQuerySet(models.QuerySet):
    def open_commitment(self, user):
        """The commitment already published to this user and not yet spent."""
        return self.filter(user=user, used_at__isnull=True).order_by("created_at").first()

    def revealed(self):
        return self.filter(revealed_at__isnull=False)


class FairnessProof(BaseModel):
    """One commit-reveal pair, and the record that lets anyone replay a round.

    The row is created at *commit* time with `server_seed` populated but never
    served: `public_dict()` withholds it until `revealed_at` is set. Storing it
    in the clear is deliberate — the secret only has to survive until the round
    settles, and an encrypted-at-rest seed we cannot produce on demand would
    make the reveal (the entire point) unreliable.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="fairness_proofs",
    )

    #: Secret until `revealed_at`. Never put this in a template or an API
    #: response without going through `public_dict()`.
    server_seed = models.CharField(max_length=128)

    #: Published at commit time, before the client seed is even known.
    server_seed_hash = models.CharField(max_length=64, db_index=True)

    #: The player's contribution. Empty while the proof is only a commitment.
    client_seed = models.CharField(max_length=CLIENT_SEED_MAX_LENGTH, blank=True)

    #: Distinguishes rounds that share a (server, client) seed pair. Also the
    #: user's own round counter, which makes "did they skip a round" checkable.
    nonce = models.PositiveIntegerField(default=0)

    purpose = models.CharField(max_length=32, blank=True)

    #: Set when the proof is bound to a round. A commitment may only be spent
    #: once; reusing one would let the server pick which of two committed seeds
    #: to apply after seeing the client seed.
    used_at = models.DateTimeField(null=True, blank=True)
    revealed_at = models.DateTimeField(null=True, blank=True)

    objects = FairnessProofQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["user", "used_at"]),
            models.Index(fields=["server_seed_hash"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["server_seed_hash", "nonce"], name="uniq_commitment_nonce"
            ),
        ]

    def __str__(self) -> str:
        return f"proof {self.server_seed_hash[:12]}… nonce={self.nonce}"

    @property
    def is_revealed(self) -> bool:
        return self.revealed_at is not None

    @property
    def is_spent(self) -> bool:
        return self.used_at is not None

    def rng(self) -> Rng:
        return Rng(self.server_seed, self.client_seed, self.nonce)

    def public_dict(self) -> dict[str, Any]:
        """Everything the client may see. The seed appears only after reveal."""
        return {
            "proof_id": str(self.pk),
            "server_seed_hash": self.server_seed_hash,
            "server_seed": self.server_seed if self.is_revealed else None,
            "client_seed": self.client_seed,
            "nonce": self.nonce,
            "revealed": self.is_revealed,
            "message": MESSAGE_TEMPLATE.format(
                client_seed=self.client_seed, nonce=self.nonce, block=0
            ),
        }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def commit(*, user=None, purpose: str = "") -> FairnessProof:
    """Publish a commitment. Call this *before* the player commits to anything.

    The returned proof carries no client seed yet — that is the player's move,
    and it must come second or the commitment proves nothing.
    """
    server_seed = new_server_seed()
    return FairnessProof.objects.create(
        user=user,
        server_seed=server_seed,
        server_seed_hash=hash_seed(server_seed),
        purpose=purpose,
    )


def commitment_for(user, *, purpose: str = "") -> FairnessProof:
    """The user's outstanding commitment, creating one if they have none.

    Idempotent by design: showing the game page twice must show the same hash,
    otherwise the player cannot tell which commitment their next round will use.
    """
    existing = FairnessProof.objects.open_commitment(user)
    if existing is not None:
        return existing
    return commit(user=user, purpose=purpose)


def bind(proof: FairnessProof, *, client_seed: str, nonce: int | None = None) -> FairnessProof:
    """Attach the player's seed and spend the commitment.

    Raises `FairnessViolation` if the commitment was already spent: a reused
    commitment would hand the server a choice it must not have.
    """
    if proof.is_spent:
        raise FairnessViolation(
            "This commitment was already used for another round. A server seed "
            "may decide exactly one outcome."
        )

    proof.client_seed = normalise_client_seed(client_seed)
    if nonce is not None:
        proof.nonce = nonce
    elif proof.user_id is not None:
        proof.nonce = FairnessProof.objects.filter(
            user_id=proof.user_id, used_at__isnull=False
        ).count()
    proof.used_at = timezone.now()
    proof.save(update_fields=["client_seed", "nonce", "used_at", "updated_at"])
    return proof


def reveal(proof: FairnessProof) -> FairnessProof:
    """Publish the server seed. Called once the round can no longer change."""
    if proof.is_revealed:
        return proof
    proof.revealed_at = timezone.now()
    proof.save(update_fields=["revealed_at", "updated_at"])
    return proof


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify(
    proof: FairnessProof,
    expected_outcome: dict[str, Any],
    *,
    derive: Callable[[Rng], dict[str, Any]] | None = None,
) -> bool:
    """Re-derive the round from the revealed seed and say whether it matches.

    Returns a plain bool because this is a *user-facing* check: the verify page
    calls it on demand and must be able to say "no" without raising. Internal
    call sites that treat a mismatch as an incident should use `assert_fair`.

    `derive` is the game's own derivation function. Omitted, it is looked up
    from the registry using ``expected_outcome["game_key"]``, so a user can
    verify a round knowing nothing but its id.
    """
    if not proof.is_revealed or not proof.server_seed:
        return False

    if hash_seed(proof.server_seed) != proof.server_seed_hash:
        # The published commitment does not match the published seed. Either the
        # seed was swapped after the fact or the row is corrupt; both are fatal.
        return False

    if derive is None:
        from games.engine import derive_for_verification

        game_key = expected_outcome.get("game_key")
        if not game_key:
            return False
        derive = derive_for_verification(game_key)

    replayed = derive(proof.rng())
    return _comparable(replayed) == _comparable(expected_outcome)


def assert_fair(
    proof: FairnessProof,
    expected_outcome: dict[str, Any],
    *,
    derive: Callable[[Rng], dict[str, Any]] | None = None,
) -> None:
    """Verify or raise. Use where a mismatch means the pipeline is compromised."""
    if not verify(proof, expected_outcome, derive=derive):
        raise FairnessViolation(
            f"Round derived from proof {proof.pk} does not match its stored outcome."
        )


def _comparable(outcome: dict[str, Any]) -> dict[str, Any]:
    """Strip presentation-only keys before comparing two derivations.

    Round ids, timestamps and how much of a ladder the player has revealed are
    not products of the RNG, so they must not take part in the comparison.
    """
    ignored = {"revealed_rungs", "cashed_out", "final_rung", "round_id", "settled_at"}
    return {k: v for k, v in outcome.items() if k not in ignored}


#: Shown on the verification page, in the user's language. Kept next to the code
#: it describes so the two cannot drift: if the derivation changes and this text
#: does not, the reviewer sees it in the same diff.
VERIFICATION_STEPS: tuple[str, ...] = (
    "Prima che la partita cominci pubblichiamo l'impronta SHA-256 del nostro seme "
    "segreto. È scritta qui sopra e non cambia più.",
    "Tu scegli il tuo seme (client seed). Puoi cambiarlo quando vuoi: lo scegli "
    "dopo aver visto la nostra impronta, quindi non possiamo adattarci a te.",
    "Il risultato nasce da HMAC-SHA256 con chiave il nostro seme e messaggio "
    "«client_seed:nonce:blocco». I byte prodotti diventano numeri interi con "
    "campionamento a rifiuto, senza alcuna distorsione.",
    "A partita conclusa riveliamo il seme segreto. Calcolane l'impronta SHA-256: "
    "deve coincidere con quella pubblicata al punto 1.",
    "Rifai l'HMAC con i tre valori e ottieni lo stesso risultato. Se non "
    "coincide, la partita è annullata e la puntata ti viene restituita.",
)

#: A complete, dependency-free reimplementation of the derivation. Shown on the
#: verify page so a sceptical user can check us with the standard library.
VERIFICATION_SNIPPET = '''import hashlib, hmac

def stream(server_seed, client_seed, nonce):
    block = 0
    while True:
        msg = f"{client_seed}:{nonce}:{block}".encode()
        yield from hmac.new(server_seed.encode(), msg, hashlib.sha256).digest()
        block += 1

def random_below(bits, n):
    width = max(4, ((n - 1).bit_length() + 7) // 8)
    space = 1 << (width * 8)
    limit = space - (space % n)
    while True:
        value = int.from_bytes(bytes(next(bits) for _ in range(width)), "big")
        if value < limit:
            return value % n

assert hashlib.sha256(SERVER_SEED.encode()).hexdigest() == PUBLISHED_HASH
'''

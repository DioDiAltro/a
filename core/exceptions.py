"""Domain exceptions shared across bounded contexts.

These are the only names one context may import from another besides
`core.events` payloads — everything else stays behind a service function.
"""


class BiglieError(Exception):
    """Base for every domain error the platform raises deliberately."""

    #: Shown to the user. Subclasses that carry sensitive detail override this.
    user_message = "Qualcosa è andato storto. Riprova."


# -- money --------------------------------------------------------------------


class InvalidAmount(BiglieError, ValueError):
    user_message = "Importo non valido."


class AssetMismatch(BiglieError, TypeError):
    user_message = "Non puoi mescolare valute diverse."


class InsufficientFunds(BiglieError):
    user_message = "Non hai abbastanza biglie."

    def __init__(self, *, required, available, message: str | None = None):
        self.required = required
        self.available = available
        super().__init__(message or f"Required {required!r}, available {available!r}.")


class LedgerImbalance(BiglieError):
    """A journal entry whose postings do not sum to zero.

    This is never a user error and never recoverable — it means the ledger was
    about to lie. The transaction is rolled back and the incident is logged for
    audit.
    """

    user_message = "Operazione annullata per sicurezza. Il tuo saldo non è stato toccato."


# -- rights and eligibility ---------------------------------------------------


class AssetNotWithdrawable(BiglieError):
    user_message = "Queste biglie non sono convertibili in denaro."


class AssetNotTransferable(BiglieError):
    user_message = "Queste biglie non possono essere regalate."


class SelfExcluded(BiglieError):
    user_message = "Hai attivato l'autoesclusione. Questa azione non è disponibile."


class KycRequired(BiglieError):
    user_message = "Verifica la tua identità per completare questa operazione."


class LimitExceeded(BiglieError):
    user_message = "Hai raggiunto il limite che hai impostato."

    def __init__(self, *, limit_name: str, message: str | None = None):
        self.limit_name = limit_name
        super().__init__(message or f"Limit {limit_name} exceeded.")


class AgeVerificationRequired(BiglieError):
    user_message = "Devi avere almeno 18 anni per questa funzione."


# -- process ------------------------------------------------------------------


class IdempotencyConflict(BiglieError):
    """Same idempotency key replayed with a different payload."""

    user_message = "Richiesta duplicata non valida."


class IllegalStateTransition(BiglieError):
    user_message = "Questa operazione non è più disponibile."

    def __init__(self, *, current: str, attempted: str, message: str | None = None):
        self.current = current
        self.attempted = attempted
        super().__init__(message or f"Cannot go from {current!r} to {attempted!r}.")


class SaleModeError(BiglieError):
    user_message = "Questa modalità di vendita non accetta l'operazione."


class FairnessViolation(BiglieError):
    """A game outcome failed its own commit-reveal verification.

    Raised by the verifier, not by normal play. If this ever fires in
    production the RNG pipeline is compromised and games must be halted.
    """

    user_message = "Partita annullata. La tua puntata è stata restituita."

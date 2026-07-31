"""Account taxonomy for the double-entry ledger."""

from __future__ import annotations

import enum


class AccountKind(enum.StrEnum):
    """What an account is *for*.

    Every posting lands in exactly one of these. The split between USER_* buckets
    is what lets a balance page answer "spendable / waiting to be claimed / tied
    up in a bet" without a single derived query.
    """

    #: Spendable right now.
    USER_AVAILABLE = "user_available"

    #: Arrived but not yet claimed. The user cannot spend this — they must go to
    #: the dashboard and choose safe or risky first. See wallet.services.claim.
    USER_PENDING = "user_pending"

    #: Committed to something in flight: a lottery ticket, an open bid, a duel
    #: stake, a purchase awaiting delivery. Still the user's money, not theirs
    #: to spend twice.
    USER_HELD = "user_held"

    # -- system accounts ----------------------------------------------------

    #: The counter-account for biglie issuance. Its balance is negative and its
    #: magnitude is exactly the biglie in circulation — the platform's liability
    #: to its users. If this ever goes positive, biglie have been destroyed
    #: without being issued and the books are wrong.
    ISSUANCE = "issuance"

    #: Where fees, rake and house edge accumulate.
    HOUSE_REVENUE = "house_revenue"

    #: Revenue earmarked for charity, before it is paid out to a beneficiary.
    CHARITY_POOL = "charity_pool"

    #: Funds the passive drip. Filled only from recognised revenue and expired
    #: credits — never from issuance. This is the constraint that stops the
    #: idle-user reward from inflating the currency.
    REDISTRIBUTION_POOL = "redistribution_pool"

    #: Prizes and pots mid-flight, owned by no user yet.
    ESCROW = "escrow"

    #: The boundary with the real banking system. Euro postings pair against it.
    EXTERNAL_FIAT = "external_fiat"

    @property
    def is_user_account(self) -> bool:
        return self.startswith("user_")


#: System accounts are singletons per asset; user accounts are per (user, asset).
SYSTEM_KINDS = frozenset(k for k in AccountKind if not k.is_user_account)


class EntryKind(enum.StrEnum):
    """Why value moved. Drives reporting, reconciliation and the AML journal."""

    DEPOSIT = "deposit"                    # euros in, biglie issued
    WITHDRAWAL = "withdrawal"              # biglie burned, euros out
    PENDING_CREDIT = "pending_credit"      # value arrives, parked
    CLAIM_SAFE = "claim_safe"              # parked -> available, minus fee
    CLAIM_RISKED = "claim_risked"          # parked -> minigame -> available or nothing
    CREDIT_EXPIRY = "credit_expiry"        # unclaimed -> redistribution pool
    HOLD = "hold"                          # available -> held
    HOLD_RELEASE = "hold_release"          # held -> available
    PURCHASE = "purchase"                  # held -> seller, fee to house
    SALE_PROCEEDS = "sale_proceeds"
    REFUND = "refund"
    GIFT = "gift"
    WAGER = "wager"                        # held -> escrow at game start
    GAME_PAYOUT = "game_payout"            # escrow -> pending credit
    DUEL_SETTLEMENT = "duel_settlement"
    FEE = "fee"
    CHARITY_ALLOCATION = "charity_allocation"
    CHARITY_PAYOUT = "charity_payout"
    DRIP = "drip"                          # redistribution pool -> user
    ADJUSTMENT = "adjustment"              # manual, admin-only, always audited


class CreditSource(enum.StrEnum):
    """Where a pending credit came from. Shown to the user on the claim card."""

    LOTTERY_PRIZE = "lottery_prize"
    AUCTION_REFUND = "auction_refund"
    SALE_PROCEEDS = "sale_proceeds"
    GAME_WIN = "game_win"
    DUEL_WIN = "duel_win"
    GIFT = "gift"
    DRIP = "drip"
    DEPOSIT = "deposit"
    REFUND = "refund"
    PROMO = "promo"

    @property
    def label(self) -> str:
        return {
            CreditSource.LOTTERY_PRIZE: "Hai vinto una lotteria",
            CreditSource.AUCTION_REFUND: "Rimborso da un'asta",
            CreditSource.SALE_PROCEEDS: "Incasso da una vendita",
            CreditSource.GAME_WIN: "Vincita al minigame",
            CreditSource.DUEL_WIN: "Hai vinto una sfida",
            CreditSource.GIFT: "Un regalo da un altro utente",
            CreditSource.DRIP: "Biglie piovute dal cielo",
            CreditSource.DEPOSIT: "Ricarica",
            CreditSource.REFUND: "Rimborso",
            CreditSource.PROMO: "Promozione",
        }[self]


class CreditState(enum.StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    #: The user gambled the credit and lost it. Kept as a distinct state from
    #: CLAIMED so "what did I actually receive" stays answerable in history.
    BURNED = "burned"
    EXPIRED = "expired"


class HoldState(enum.StrEnum):
    ACTIVE = "active"
    CAPTURED = "captured"
    RELEASED = "released"

"""Tuning for the drip, the RTP watchdog and the inflation alarm.

Every number here is a *bound*. The drip is the feature the brief asked for —
"an idle user can still earn a few biglie at random" — and it is also the single
most dangerous thing in the product, because a reward that arrives without the
user doing anything is a reward nobody is watching. So it is fenced on four
sides: who is eligible, how much per award, how much per user per day, and how
much per run.

None of these bounds is what makes the drip non-inflationary. That comes from
`wallet.services.drip`, which pays out of REDISTRIBUTION_POOL and returns None
when the pool is dry. These bounds are what stop one run from emptying the pool
into ten accounts and calling it luck.
"""

from __future__ import annotations

from datetime import timedelta

from core.money import Asset

# ---------------------------------------------------------------------------
# The passive drip
# ---------------------------------------------------------------------------

#: The drip pays biglie *vinte*: platform-produced, spendable, wagerable, never
#: withdrawable. Handing out withdrawable BIGLIA_PIENA for free would turn the
#: feature into a faucet that converts to euros, which is both a redemption
#: liability and the shape of a laundering rail.
DRIP_ASSET = Asset.BIGLIA_VINTA

#: A "few scaglie". Small on purpose: the pleasure is in the notification, not
#: in the amount, and a drip large enough to matter is a drip large enough to
#: farm with throwaway accounts.
DRIP_MIN_AWARD_MINOR = 1
DRIP_MAX_AWARD_MINOR = 25

#: Ceiling per user per rolling day. One biglia. Reached only by a user the
#: random draw favoured repeatedly, and it stops there.
DRIP_DAILY_CAP_PER_USER_MINOR = 100

#: A single run may spend at most this share of the pool, and never more than
#: the absolute ceiling. Beat runs this every five minutes; without the share
#: cap a freshly funded pool would be emptied by the next tick.
DRIP_RUN_BUDGET_BASIS_POINTS = 200  # 2% of the pool
DRIP_RUN_MAX_MINOR = 5_000  # 50 biglie

#: How many people can win in one run.
DRIP_MAX_RECIPIENTS_PER_RUN = 25

#: How many candidates to pull before weighting. Bounds the work per run on a
#: large user table; the random slice is what keeps it fair across runs.
DRIP_CANDIDATE_POOL = 500

#: An account must have existed this long. Registration is free, so without an
#: age floor the drip pays whoever can create email addresses fastest.
DRIP_MIN_ACCOUNT_AGE = timedelta(days=3)

#: Accounts not seen for longer than this stop being dripped on. Money parked in
#: an abandoned account is a liability that never converts into anything.
DRIP_STALE_AFTER = timedelta(days=30)

#: Minimum gap between two awards to the same person, so a run cannot follow the
#: previous one straight back to the same lucky account.
DRIP_USER_COOLDOWN = timedelta(hours=4)

#: Weighting. Weight grows with how long it has been since the user last had a
#: drip (or since they joined, if never). Capped so that an account dormant for
#: a year is favoured, not guaranteed — the feature has to feel like luck.
DRIP_WEIGHT_HOURS_CAP = 168  # one week
DRIP_WEIGHT_HOURS_PER_STEP = 6

# ---------------------------------------------------------------------------
# RTP surveillance
# ---------------------------------------------------------------------------

#: The window the six-hourly audit looks back over.
RTP_AUDIT_WINDOW = timedelta(hours=6)

#: Below this many rounds, realised RTP is noise. The audit still records the
#: row — the history is worth having — but it does not raise an alert.
RTP_MIN_ROUNDS_FOR_ALERT = 200

#: How far realised may drift from configured before it is an incident.
#: 500bp = five percentage points, i.e. a 94% game realising outside 89–99%
#: over the window.
RTP_DRIFT_TOLERANCE_BP = 500

#: `games` declares each game's RTP on its own definition, but `economy` is not
#: allowed to import `games` (see tests/test_boundaries.py), and `GameSettled`
#: does not carry the configured figure. Overrides go here or in
#: `settings.ECONOMY_CONFIGURED_RTP_BP`; anything not listed is compared against
#: the platform default from `core.contracts`.
CONFIGURED_RTP_BP: dict[str, int] = {}

#: Key used for rounds that came out of the claim ritual rather than a minigame.
#: The claim gamble runs on the same edge and deserves the same watchdog.
CLAIM_GAMBLE_KEY = "claim_gamble"

# ---------------------------------------------------------------------------
# Inflation surveillance
# ---------------------------------------------------------------------------

#: Default window for `monitoring.inflation_alert`.
INFLATION_WINDOW = timedelta(days=7)

#: The house paying out more than it earns over a full window means an outcome
#: table is wrong. A little slack for variance; not much, because the whole
#: point is to catch it before it compounds.
BANKROLL_BLEED_TOLERANCE_BASIS_POINTS = 12_000  # paid may reach 120% of revenue

#: Below this much recognised revenue the ratios are meaningless.
MIN_REVENUE_FOR_ALERT_MINOR = 10_000  # 100 biglie

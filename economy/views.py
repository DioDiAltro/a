"""The staff dashboard.

Staff only, and the check is `is_staff` on the view rather than a URL somebody
has to remember not to link: these figures say how much the house is making and
where the bankroll stands.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import render

from economy.constants import DRIP_ASSET, INFLATION_WINDOW
from economy.models import DripAward, EconomySnapshot, RtpAudit
from economy.monitoring import circulating_supply, inflation_alert, sources_and_sinks

staff_only = user_passes_test(lambda user: user.is_active and user.is_staff)


@staff_only
def dashboard(request):
    """Sources against sinks, the RTP watchdog's last verdicts, and the drip."""
    window = _requested_window(request)
    figures = sources_and_sinks(window=window)

    latest_audits = _latest_audit_per_game()

    return render(
        request,
        "economy/dashboard.html",
        {
            "figures": figures,
            "alert": inflation_alert(window=window),
            "supply": circulating_supply(),
            "audits": latest_audits,
            "drift_audits": [a for a in latest_audits if a.is_alert],
            "recent_drips": list(
                DripAward.objects.select_related("user")[:20]
            ),
            "snapshots": list(EconomySnapshot.objects.all()[:10]),
            "window_days": int(window.total_seconds() // 86400),
            "drip_asset": DRIP_ASSET,
        },
    )


def _latest_audit_per_game() -> list[RtpAudit]:
    """The most recent verdict for each game.

    A plain "last 20 rows" would show the same game repeatedly and hide a game
    that stopped being audited, which is the one thing worth noticing.
    """
    seen: dict[str, RtpAudit] = {}
    for audit in RtpAudit.objects.all()[:200]:
        seen.setdefault(audit.game_key, audit)
    return sorted(seen.values(), key=lambda a: a.drift_bp)


def _requested_window(request) -> timedelta:
    raw = request.GET.get("giorni")
    if raw and raw.isdigit():
        return timedelta(days=max(1, min(int(raw), 365)))
    return INFLATION_WINDOW

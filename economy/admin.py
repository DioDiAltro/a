"""Read-only back office.

Every model in this context is a recording of something that already happened.
There is nothing here to edit, and an editable surveillance table is a
surveillance table nobody should believe.
"""

from __future__ import annotations

from django.contrib import admin

from economy.models import (
    DripAward,
    EconomySnapshot,
    GameRoundOutcome,
    MintRecord,
    RevenueRecord,
    RtpAudit,
    WithdrawalRecord,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(RevenueRecord)
class RevenueRecordAdmin(ReadOnlyAdmin):
    list_display = ("occurred_at", "source", "amount_minor", "asset",
                    "charity_minor", "redistribution_minor")
    list_filter = ("source", "asset")
    date_hierarchy = "occurred_at"


@admin.register(MintRecord)
class MintRecordAdmin(ReadOnlyAdmin):
    list_display = ("occurred_at", "biglie_minor", "euro_minor", "user_id")
    date_hierarchy = "occurred_at"


@admin.register(WithdrawalRecord)
class WithdrawalRecordAdmin(ReadOnlyAdmin):
    list_display = ("occurred_at", "euro_minor", "user_id")
    date_hierarchy = "occurred_at"


@admin.register(GameRoundOutcome)
class GameRoundOutcomeAdmin(ReadOnlyAdmin):
    list_display = ("settled_at", "game_key", "origin", "stake_minor", "payout_minor", "asset")
    list_filter = ("game_key", "origin", "asset")
    date_hierarchy = "settled_at"


@admin.register(DripAward)
class DripAwardAdmin(ReadOnlyAdmin):
    list_display = ("awarded_at", "user", "amount_minor", "asset", "weight", "run_id")
    list_filter = ("asset",)
    search_fields = ("user__email",)
    date_hierarchy = "awarded_at"


@admin.register(RtpAudit)
class RtpAuditAdmin(ReadOnlyAdmin):
    list_display = ("window_end", "game_key", "rounds", "realised_bp",
                    "configured_bp", "drift_bp", "is_alert")
    list_filter = ("game_key", "is_alert")
    date_hierarchy = "window_end"


@admin.register(EconomySnapshot)
class EconomySnapshotAdmin(ReadOnlyAdmin):
    list_display = ("taken_at", "minted_minor", "revenue_minor", "drip_minor",
                    "house_paid_minor", "alert")
    list_filter = ("alert",)
    date_hierarchy = "taken_at"

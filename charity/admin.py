"""Back office for the 3%.

Beneficiaries are editable — somebody has to name them. Allocations and payout
amounts are not: they are the record the public page is built from, and a
back office that can retouch them makes the page worthless.
"""

from __future__ import annotations

from django.contrib import admin, messages

from charity.constants import PayoutState
from charity.models import Beneficiary, CharityAllocation, CharityPayout
from charity.services import confirm_payout, distribute_pool
from core.exceptions import BiglieError


@admin.register(Beneficiary)
class BeneficiaryAdmin(admin.ModelAdmin):
    list_display = ("name", "weight", "is_active", "registration_number", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "registration_number")
    prepopulated_fields = {"slug": ("name",)}

    actions = ("run_distribution",)

    @admin.action(description="Ripartisci il fondo beneficenza tra le associazioni attive")
    def run_distribution(self, request, queryset):
        """Split whatever is distributable right now.

        Ignores the selection on purpose: a distribution is over the whole pool
        and every active beneficiary, never over the rows somebody happened to
        tick.
        """
        payouts = distribute_pool()
        if not payouts:
            self.message_user(
                request, "Niente da ripartire in questo momento.", messages.INFO
            )
            return
        total = sum(p.amount_minor for p in payouts)
        self.message_user(
            request,
            f"{len(payouts)} quote create per un totale di {total} unità minori.",
            messages.SUCCESS,
        )


@admin.register(CharityAllocation)
class CharityAllocationAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "amount_minor", "asset", "basis_points")
    list_filter = ("asset",)
    date_hierarchy = "occurred_at"
    readonly_fields = tuple(f.name for f in CharityAllocation._meta.fields)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(CharityPayout)
class CharityPayoutAdmin(admin.ModelAdmin):
    list_display = ("created_at", "beneficiary", "amount_minor", "asset", "state", "reference")
    list_filter = ("state", "asset", "beneficiary")
    search_fields = ("reference", "beneficiary__name")
    readonly_fields = ("beneficiary", "amount_minor", "asset", "batch",
                       "weight_at_distribution", "paid_at", "created_at", "updated_at")

    def has_add_permission(self, request) -> bool:
        # Payouts are created by a distribution run, never by hand: a hand-made
        # payout would not tie back to the pool.
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    @admin.action(description="Segna come versato (richiede il riferimento del bonifico)")
    def mark_paid(self, request, queryset):
        confirmed = 0
        for payout in queryset.filter(state=PayoutState.INSTRUCTED):
            if not payout.reference:
                self.message_user(
                    request,
                    f"{payout} non ha il riferimento del bonifico: compilalo prima.",
                    messages.WARNING,
                )
                continue
            try:
                confirm_payout(payout, reference=payout.reference)
            except BiglieError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                confirmed += 1
        if confirmed:
            self.message_user(request, f"{confirmed} versamenti confermati.", messages.SUCCESS)

    actions = ("mark_paid",)

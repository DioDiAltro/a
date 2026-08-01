"""Admin for the regulated surface.

Two rules shape this file:

* **Nothing money-moving is editable.** Deposits, withdrawals and provider
  events are records of what happened, not forms. Changing a state by hand
  would desynchronise the row from the ledger entry that proves it, so every
  transition goes through `payments.services` — including the two admin actions
  below, which are the manual-review queue rather than a way around it.

* **The review queue is the default view.** A withdrawal waiting on a human is
  somebody's money sitting still, so `Withdrawal` opens filtered on exactly
  that.
"""

from __future__ import annotations

from django.contrib import admin, messages

from core.exceptions import BiglieError
from payments.constants import PaymentState
from payments.models import Deposit, ProviderEventLog, Withdrawal
from payments.services import approve_withdrawal, reject_withdrawal


@admin.register(Deposit)
class DepositAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "euro_display", "biglie_display", "state",
                    "provider", "provider_reference")
    list_filter = ("state", "provider", "created_at")
    search_fields = ("user__email", "provider_reference", "id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    # The client secret is deliberately not in the list or the detail form: it
    # is a bearer token for the payment, and an admin screen is not where it
    # belongs.
    fields = ("user", "euro_minor", "biglie_minor", "state", "provider",
              "provider_reference", "failure_code", "failure_message",
              "confirmed_at", "failed_at", "reversed_at", "metadata", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(description="Euro", ordering="euro_minor")
    def euro_display(self, obj: Deposit) -> str:
        return str(obj.euro)

    @admin.display(description="Biglie", ordering="biglie_minor")
    def biglie_display(self, obj: Deposit) -> str:
        return str(obj.biglie)


@admin.register(Withdrawal)
class WithdrawalAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "biglie_display", "euro_display", "state",
                    "requires_review", "destination_last4", "reviewed_by")
    list_filter = ("state", "requires_review", "approved_automatically", "created_at")
    search_fields = ("user__email", "provider_reference", "id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    actions = ("approve_selected", "reject_selected")

    fields = ("user", "biglie_minor", "asset", "euro_minor", "state",
              "destination_reference", "destination_last4", "hold_id",
              "requires_review", "approved_automatically", "reviewed_by", "reviewed_at",
              "rejection_reason", "provider", "provider_reference",
              "confirmed_at", "completed_at", "failed_at", "reversed_at", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def get_changelist_instance(self, request):
        # Land on the queue that needs a person, not on everything ever paid.
        if not request.GET and "requires_review" not in request.GET:
            request.GET = request.GET.copy()
            request.GET["state__exact"] = PaymentState.PENDING
            request.META["QUERY_STRING"] = request.GET.urlencode()
        return super().get_changelist_instance(request)

    @admin.display(description="Biglie", ordering="biglie_minor")
    def biglie_display(self, obj: Withdrawal) -> str:
        return str(obj.biglie)

    @admin.display(description="Euro", ordering="euro_minor")
    def euro_display(self, obj: Withdrawal) -> str:
        return str(obj.euro)

    @admin.action(description="Approva e paga i prelievi selezionati")
    def approve_selected(self, request, queryset) -> None:
        paid = 0
        for withdrawal in queryset.filter(state=PaymentState.PENDING):
            try:
                approve_withdrawal(withdrawal, approved_by=request.user)
            except BiglieError as exc:
                self.message_user(
                    request, f"{withdrawal.id}: {exc}", level=messages.ERROR
                )
            else:
                paid += 1
        self.message_user(request, f"{paid} prelievi approvati e inviati.")

    @admin.action(description="Rifiuta i prelievi selezionati")
    def reject_selected(self, request, queryset) -> None:
        """Refusal releases the hold — the biglie go back to the user.

        This is the *only* way a confirmed withdrawal stops, and it exists for
        compliance reasons, never as a "ci ripensi?" offered to the user.
        """
        rejected = 0
        for withdrawal in queryset.filter(state=PaymentState.PENDING):
            try:
                reject_withdrawal(
                    withdrawal,
                    reason="Respinto dopo revisione manuale.",
                    rejected_by=request.user,
                )
            except BiglieError as exc:
                self.message_user(
                    request, f"{withdrawal.id}: {exc}", level=messages.ERROR
                )
            else:
                rejected += 1
        self.message_user(request, f"{rejected} prelievi respinti; le biglie sono tornate.")


@admin.register(ProviderEventLog)
class ProviderEventLogAdmin(admin.ModelAdmin):
    """The signed evidence behind every credit. Read-only, always."""

    list_display = ("created_at", "provider", "event_type", "event_id", "handled", "outcome")
    list_filter = ("provider", "event_type", "handled")
    search_fields = ("event_id", "outcome")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("provider", "event_id", "event_type", "payload", "handled",
                       "outcome", "deposit", "withdrawal", "created_at")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

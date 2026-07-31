"""Admin for accounts.

Deliberately read-mostly. Editing a balance-adjacent field from the admin is how
an audit trail acquires holes; the money lives in `wallet` and moves through
services that write journal entries.
"""

from __future__ import annotations

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from identity.models import PlayerLevel, SellerProfile, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("email",)
    list_display = ("email", "display_name", "is_identity_verified", "is_self_excluded",
                    "is_active", "date_joined")
    list_filter = ("is_identity_verified", "is_self_excluded", "is_active", "is_staff")
    search_fields = ("email", "display_name")
    readonly_fields = ("last_login", "date_joined", "last_seen_at")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profilo", {"fields": ("display_name", "avatar", "date_of_birth")}),
        ("Conformità", {
            "fields": ("is_identity_verified", "is_self_excluded"),
            "description": (
                "Questi due flag sono specchi dello stato in `compliance`. "
                "Modificali da qui solo per correggere una desincronizzazione."
            ),
        }),
        ("Permessi", {"fields": ("is_active", "is_staff", "is_superuser", "groups",
                                 "user_permissions")}),
        ("Date", {"fields": ("last_login", "date_joined", "last_seen_at")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "date_of_birth", "password1", "password2"),
        }),
    )


@admin.register(SellerProfile)
class SellerProfileAdmin(admin.ModelAdmin):
    list_display = ("shop_name", "user", "sales_count", "rating", "accepts_duels")
    search_fields = ("shop_name", "user__email")
    list_filter = ("accepts_duels",)


@admin.register(PlayerLevel)
class PlayerLevelAdmin(admin.ModelAdmin):
    list_display = ("user", "number", "title", "xp", "leveled_up_at")
    list_filter = ("number",)
    search_fields = ("user__email",)
    readonly_fields = ("xp", "number", "leveled_up_at", "updated_at")

    @admin.display(description="Titolo")
    def title(self, obj: PlayerLevel) -> str:
        return obj.title

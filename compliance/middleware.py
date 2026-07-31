"""Self-exclusion enforcement.

A user who has excluded themselves must not merely be unable to log in — they
must be unable to reach any surface that solicits a wager or a deposit, on every
request, without each view having to remember. Middleware is the right place
because forgetting is exactly the failure mode that makes self-exclusion
worthless.

Read-only surfaces stay open on purpose: someone who excluded themselves still
needs to reach support, their order history and their withdrawal page.
"""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import resolve

#: URL namespaces that solicit play or spending. Blocked while excluded.
RESTRICTED_NAMESPACES = {"games", "marketplace", "payments"}

#: Named routes inside those namespaces that stay reachable — getting money
#: *out* and reading what already happened must never be blocked.
ALWAYS_ALLOWED_ROUTES = {
    "payments:withdraw",
    "payments:withdraw_confirm",
    "marketplace:order_list",
    "marketplace:order_detail",
    "games:history",
    "games:verify",
}


class SelfExclusionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)

        if user is not None and user.is_authenticated and user.is_self_excluded:
            match = resolve(request.path_info)
            route = f"{match.namespace}:{match.url_name}" if match.namespace else match.url_name

            if match.namespace in RESTRICTED_NAMESPACES and route not in ALWAYS_ALLOWED_ROUTES:
                messages.warning(
                    request,
                    "Hai attivato l'autoesclusione. Puoi consultare i tuoi ordini e "
                    "richiedere un prelievo, ma non giocare o acquistare.",
                )
                return redirect("compliance:self_exclusion_status")

        return self.get_response(request)

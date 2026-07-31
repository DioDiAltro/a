"""Boot-time system checks.

The platform settles games of chance and moves real euros. A deployment that
has LIVE_MODE on but compliance switched off is not a misconfiguration to warn
about — it is an unlicensed gambling operation. These checks are errors, and
Django refuses to serve when they trip.
"""

from django.conf import settings
from django.core.checks import Error, Warning, register

# Every one of these must be truthy before the platform may take real money.
LIVE_REQUIREMENTS = [
    ("STRIPE_SECRET_KEY", "payment processing cannot settle without it"),
    ("STRIPE_WEBHOOK_SECRET", "unverified webhooks would let anyone credit a wallet"),
    ("ADM_CONCESSION_NUMBER", "operating games of chance for money requires a licence"),
    ("KYC_PROVIDER_API_KEY", "withdrawals must be identity-verified"),
]


@register()
def check_live_mode_is_fully_configured(app_configs, **kwargs):
    """Refuse to boot a live deployment that is missing a compliance control."""
    problems = []

    if not getattr(settings, "LIVE_MODE", False):
        return problems

    for name, why in LIVE_REQUIREMENTS:
        if not getattr(settings, name, ""):
            problems.append(
                Error(
                    f"LIVE_MODE is on but {name} is not set.",
                    hint=(
                        f"{why.capitalize()}. Set {name}, or turn LIVE_MODE off to run "
                        "the platform in play-money mode."
                    ),
                    id="biglie.E001",
                )
            )

    if settings.DEBUG:
        problems.append(
            Error(
                "LIVE_MODE and DEBUG are both on.",
                hint="Never expose tracebacks on a deployment holding customer funds.",
                id="biglie.E002",
            )
        )

    return problems


@register()
def check_ledger_database_is_transactional(app_configs, **kwargs):
    """The ledger's zero-sum guarantee relies on real transactions."""
    engine = settings.DATABASES["default"]["ENGINE"]
    if "postgresql" not in engine and not settings.DEBUG:
        return [
            Warning(
                f"The ledger is running on {engine}.",
                hint=(
                    "Double-entry balance is enforced with a deferred PostgreSQL "
                    "constraint trigger. On other backends the invariant degrades to "
                    "application-level checks only."
                ),
                id="biglie.W001",
            )
        ]
    return []

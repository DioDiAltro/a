"""The boot gate.

`config.checks` is what stands between "somebody set LIVE_MODE in the deploy
config" and an unlicensed gambling operation taking real cards with an
unverified webhook endpoint. These tests are here, in `payments`, because three
of the four requirements are this context's.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from config.checks import check_live_mode_is_fully_configured
from payments.providers import (
    FakeProvider,
    StripeProvider,
    get_provider,
    reset_fake_provider,
)

LIVE_SECRETS = {
    "STRIPE_SECRET_KEY": "sk_live_x",
    "STRIPE_WEBHOOK_SECRET": "whsec_live_x",
    "ADM_CONCESSION_NUMBER": "ADM-000000",
    "KYC_PROVIDER_API_KEY": "kyc_live_x",
}


@override_settings(LIVE_MODE=True, DEBUG=False, **dict.fromkeys(LIVE_SECRETS, ""))
def test_live_mode_without_secrets_refuses_to_boot():
    """Every missing control is its own error, so the log names all four."""
    problems = check_live_mode_is_fully_configured(None)

    assert len(problems) == len(LIVE_SECRETS)
    assert {problem.id for problem in problems} == {"biglie.E001"}

    reported = " ".join(problem.msg for problem in problems)
    for name in LIVE_SECRETS:
        assert name in reported


@override_settings(LIVE_MODE=True, DEBUG=False, STRIPE_SECRET_KEY="sk_live_x",
                   STRIPE_WEBHOOK_SECRET="", ADM_CONCESSION_NUMBER="ADM-1",
                   KYC_PROVIDER_API_KEY="kyc")
def test_a_missing_webhook_secret_alone_is_still_fatal():
    """Without it, anyone who finds the callback URL can credit a wallet."""
    problems = check_live_mode_is_fully_configured(None)

    assert len(problems) == 1
    assert "STRIPE_WEBHOOK_SECRET" in problems[0].msg


@override_settings(LIVE_MODE=True, DEBUG=True, **LIVE_SECRETS)
def test_live_mode_with_debug_on_is_refused():
    problems = check_live_mode_is_fully_configured(None)

    assert [problem.id for problem in problems] == ["biglie.E002"]


@override_settings(LIVE_MODE=True, DEBUG=False, **LIVE_SECRETS)
def test_a_fully_configured_live_deployment_boots():
    assert check_live_mode_is_fully_configured(None) == []


@override_settings(LIVE_MODE=False, STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="")
def test_play_money_mode_needs_no_secrets():
    assert check_live_mode_is_fully_configured(None) == []


# ---------------------------------------------------------------------------
# The same guard, one layer down
# ---------------------------------------------------------------------------


def test_the_fake_provider_refuses_to_run_in_live_mode(settings):
    """Settling payments against an in-memory dict would be a free mint."""
    settings.LIVE_MODE = True
    settings.STRIPE_SECRET_KEY = ""
    reset_fake_provider()

    with pytest.raises(ImproperlyConfigured):
        get_provider()


def test_the_provider_is_chosen_by_configuration(settings):
    settings.LIVE_MODE = False
    settings.STRIPE_SECRET_KEY = ""
    reset_fake_provider()
    assert isinstance(get_provider(), FakeProvider)

    settings.STRIPE_SECRET_KEY = "sk_test_x"
    assert isinstance(get_provider(), StripeProvider)

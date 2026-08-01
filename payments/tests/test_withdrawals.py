"""Getting money out.

The rules being defended here are the ones docs/ECONOMY.md calls
non-negotiable: only biglie bought with real money may leave, leaving destroys
supply, and nothing about the flow tries to talk the user out of it.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from core.exceptions import (
    AssetNotWithdrawable,
    IllegalStateTransition,
    InsufficientFunds,
    InvalidAmount,
    KycRequired,
)
from core.money import Asset
from payments.constants import AUTO_APPROVE_MAX_MINOR, PaymentState
from payments.models import Withdrawal
from payments.services import (
    approve_withdrawal,
    available_piena,
    confirm_withdrawal,
    euro_for_biglie,
    open_withdrawal,
    reject_withdrawal,
    reverse_withdrawal,
)
from payments.tests.conftest import DONO, PIENA, TEST_IBAN, VINTA, biglie
from wallet.ledger import circulating_supply, reconcile

# ---------------------------------------------------------------------------
# The line that keeps the platform from being a laundry
# ---------------------------------------------------------------------------


def test_winnings_cannot_be_withdrawn_and_the_refusal_says_why(db, funded):
    """Refusing is half the job; explaining is the other half.

    A bare "non consentito" reads as the platform keeping the money. The
    message has to name what can be withdrawn, what cannot, and the reason —
    otherwise the rule looks arbitrary and support pays for it forever.
    """
    with pytest.raises(AssetNotWithdrawable) as caught:
        open_withdrawal(user=funded, biglie=biglie("40", VINTA), iban=TEST_IBAN)

    message = caught.value.user_message
    assert "biglie piene" in message
    assert "riciclaggio" in message
    assert "denaro reale" in message
    # And nothing was created on the way to the refusal.
    assert Withdrawal.objects.count() == 0


def test_gifts_cannot_be_withdrawn_either(db, funded):
    """The asset swap in `gift` is pointless if DONO can walk out as euros."""
    with pytest.raises(AssetNotWithdrawable):
        open_withdrawal(user=funded, biglie=biglie("10", DONO), iban=TEST_IBAN)


def test_the_conversion_helper_refuses_non_withdrawable_assets(db):
    with pytest.raises(AssetNotWithdrawable):
        euro_for_biglie(biglie("10", VINTA))


def test_a_withdrawal_row_can_only_hold_piena(db, funded):
    withdrawal = open_withdrawal(user=funded, biglie=biglie("20"), iban=TEST_IBAN)
    assert withdrawal.asset == Asset.BIGLIA_PIENA


# ---------------------------------------------------------------------------
# The happy path — and what it does to the supply
# ---------------------------------------------------------------------------


def test_small_withdrawal_from_a_verified_user_is_paid_and_burns_supply(db, funded):
    before = circulating_supply(PIENA)

    withdrawal = open_withdrawal(user=funded, biglie=biglie("50"), iban=TEST_IBAN)
    withdrawal = confirm_withdrawal(withdrawal)

    assert withdrawal.state == PaymentState.SUCCEEDED
    assert withdrawal.approved_automatically is True
    assert withdrawal.provider_reference.startswith("po_fake_")

    # The supply is gone, not moved: this is the only deflationary path.
    assert circulating_supply(PIENA) == before - biglie("50")
    assert available_piena(funded) == biglie("445")
    assert reconcile(record_anomalies=False) == []


def test_the_iban_is_not_stored(db, funded):
    """Only the last four digits and a non-reversible fingerprint survive."""
    withdrawal = open_withdrawal(user=funded, biglie=biglie("20"), iban=TEST_IBAN)

    assert withdrawal.destination_last4 == TEST_IBAN[-4:]
    assert TEST_IBAN not in withdrawal.destination_reference
    assert withdrawal.destination_reference.startswith("ban_")


def test_withdrawal_publishes_the_requested_event(
    db, funded, django_capture_on_commit_callbacks
):
    """Publishing correctly is all `payments` has to do to get AML logging."""
    from compliance.models import AuditEvent

    withdrawal = open_withdrawal(user=funded, biglie=biglie("30"), iban=TEST_IBAN)
    with django_capture_on_commit_callbacks(execute=True):
        confirm_withdrawal(withdrawal)

    assert AuditEvent.objects.filter(event_name="payments.withdrawal_requested").count() == 1


# ---------------------------------------------------------------------------
# Compliance gates
# ---------------------------------------------------------------------------


def test_an_unverified_user_cannot_withdraw(db, funded_unverified):
    """No KYC, no payout — checked on the draft, before any money is reserved."""
    with pytest.raises(KycRequired):
        open_withdrawal(user=funded_unverified, biglie=biglie("20"), iban=TEST_IBAN)

    assert Withdrawal.objects.count() == 0


def test_you_cannot_withdraw_more_than_you_have(db, verified):
    with pytest.raises(InsufficientFunds):
        open_withdrawal(user=verified, biglie=biglie("10"), iban=TEST_IBAN)


def test_amounts_outside_the_bounds_are_refused(db, funded):
    with pytest.raises(InvalidAmount):
        open_withdrawal(user=funded, biglie=biglie("0.50"), iban=TEST_IBAN)


# ---------------------------------------------------------------------------
# Manual review
# ---------------------------------------------------------------------------


def test_a_large_withdrawal_waits_for_a_human_and_holds_the_money(db, funded):
    """Waiting must never mean the money is spendable in the meantime."""
    large = biglie(str(AUTO_APPROVE_MAX_MINOR // 100 + 50))
    before = circulating_supply(PIENA)

    withdrawal = confirm_withdrawal(
        open_withdrawal(user=funded, biglie=large, iban=TEST_IBAN)
    )

    assert withdrawal.state == PaymentState.PENDING
    assert withdrawal.requires_review is True
    assert withdrawal.hold_id is not None
    # Held, so it cannot be re-spent while a reviewer looks...
    assert available_piena(funded) == biglie("495") - large
    # ...and nothing has been burned yet.
    assert circulating_supply(PIENA) == before

    approve_withdrawal(withdrawal, approved_by=None)
    withdrawal.refresh_from_db()

    assert withdrawal.state == PaymentState.SUCCEEDED
    assert circulating_supply(PIENA) == before - large
    assert reconcile(record_anomalies=False) == []


def test_rejecting_a_withdrawal_gives_every_scaglia_back(db, funded):
    large = biglie(str(AUTO_APPROVE_MAX_MINOR // 100 + 50))
    before_supply = circulating_supply(PIENA)

    withdrawal = confirm_withdrawal(
        open_withdrawal(user=funded, biglie=large, iban=TEST_IBAN)
    )
    reject_withdrawal(withdrawal, reason="Documenti da aggiornare.")
    withdrawal.refresh_from_db()

    assert withdrawal.state == PaymentState.FAILED
    assert available_piena(funded) == biglie("495")
    assert circulating_supply(PIENA) == before_supply
    assert reconcile(record_anomalies=False) == []


def test_a_withdrawal_cannot_be_confirmed_twice(db, funded):
    withdrawal = open_withdrawal(user=funded, biglie=biglie("30"), iban=TEST_IBAN)
    confirm_withdrawal(withdrawal)

    with pytest.raises(IllegalStateTransition):
        confirm_withdrawal(withdrawal)


def test_approving_a_settled_withdrawal_is_a_no_op(db, funded):
    """The double-clicked admin action must not pay twice."""
    withdrawal = confirm_withdrawal(
        open_withdrawal(user=funded, biglie=biglie("30"), iban=TEST_IBAN)
    )
    supply = circulating_supply(PIENA)

    approve_withdrawal(withdrawal)

    assert circulating_supply(PIENA) == supply


# ---------------------------------------------------------------------------
# When the payout bounces
# ---------------------------------------------------------------------------


def test_a_bounced_payout_reissues_through_the_only_mint(db, funded):
    """The euros came back, so the biglie must too — via `issue_for_deposit`.

    Any other route would be a second mint, and the platform has exactly one.
    """
    before = circulating_supply(PIENA)
    withdrawal = confirm_withdrawal(
        open_withdrawal(user=funded, biglie=biglie("50"), iban=TEST_IBAN)
    )
    assert circulating_supply(PIENA) == before - biglie("50")

    reverse_withdrawal(withdrawal, reason="IBAN rifiutato dalla banca")
    withdrawal.refresh_from_db()

    assert withdrawal.state == PaymentState.REVERSED
    assert circulating_supply(PIENA) == before
    assert reconcile(record_anomalies=False) == []


# ---------------------------------------------------------------------------
# The dark pattern we are not building
# ---------------------------------------------------------------------------


def test_there_is_no_way_for_a_user_to_cancel_a_withdrawal(db):
    """The missing "annulla il prelievo e continua a giocare" is not an oversight.

    It is the highest-converting dark pattern in gambling products, and it works
    by catching people at the exact moment they decided to stop. This test is
    the tripwire: adding such a route or service makes it fail.
    """
    from payments import services, urls

    route_names = {pattern.name for pattern in urls.urlpatterns}
    assert not {name for name in route_names if "cancel" in name or "annulla" in name}

    exported = set(dir(services))
    assert "cancel_withdrawal" not in exported
    assert "resume_play" not in exported

    # Only a reviewer can stop a confirmed withdrawal, and only with a reason.
    assert "reject_withdrawal" in exported


def test_withdrawal_routes_stay_reachable_while_self_excluded(client, funded):
    """The compliance middleware allowlist and these route names are one contract.

    A self-excluded user must be able to take their money out. If either side
    renames a route, this test is what notices.
    """
    funded.is_self_excluded = True
    funded.save(update_fields=["is_self_excluded"])
    client.force_login(funded)

    withdrawal = open_withdrawal(user=funded, biglie=biglie("30"), iban=TEST_IBAN)

    assert client.get(reverse("payments:withdraw")).status_code == 200
    assert client.get(
        reverse("payments:withdraw_confirm", args=[withdrawal.pk])
    ).status_code == 200

    # ...while the surfaces that solicit spending stay shut.
    blocked = client.get(reverse("payments:deposit"))
    assert blocked.status_code == 302
    assert reverse("compliance:self_exclusion_status") in blocked.url


# ---------------------------------------------------------------------------
# Through the interface
# ---------------------------------------------------------------------------


def test_the_withdrawal_screens_walk_end_to_end(client, funded):
    client.force_login(funded)

    posted = client.post(
        reverse("payments:withdraw"), {"biglie": "40.00", "iban": TEST_IBAN}
    )
    assert posted.status_code == 302

    withdrawal = Withdrawal.objects.get(user=funded)
    assert withdrawal.state == PaymentState.INITIATED

    confirm_url = reverse("payments:withdraw_confirm", args=[withdrawal.pk])
    assert posted.url == confirm_url

    page = client.get(confirm_url)
    assert page.status_code == 200
    assert "Conferma il prelievo" in page.content.decode()

    confirmed = client.post(confirm_url)
    assert confirmed.status_code == 302

    withdrawal.refresh_from_db()
    assert withdrawal.state == PaymentState.SUCCEEDED
    assert circulating_supply(PIENA) == biglie("460")
    assert available_piena(funded) == biglie("455")


def test_the_withdraw_page_names_what_cannot_be_withdrawn(client, funded):
    """The explanation belongs on the page, before the refusal."""
    client.force_login(funded)

    body = client.get(reverse("payments:withdraw")).content.decode()

    assert "biglie piene" in body


def test_an_invalid_iban_is_caught_before_anything_moves(client, funded):
    client.force_login(funded)

    response = client.post(
        reverse("payments:withdraw"), {"biglie": "40.00", "iban": "IT60X0542811101000000123457"}
    )

    assert response.status_code == 200  # re-rendered with the error
    assert Withdrawal.objects.count() == 0

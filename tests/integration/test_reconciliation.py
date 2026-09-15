"""
Integration tests for app/services/reconciliation.py.

Per ADR 0005, assertions never claim the whole database is drift-free —
other tests in this file deliberately inject drift into their own accounts,
and that drift is never cleaned up (no DB reset between runs). Every
assertion here is scoped to the specific accounts a given test created.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import update

from app.db.base import SessionLocal
from app.db.models import Balance
from app.services.reconciliation import reconcile
from tests.integration.conftest import authorize

pytestmark = pytest.mark.integration


async def test_reconciliation_reports_no_drift_for_normal_activity(client, payer_payee):
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=5_000)
    captured = await client.post(f"/payments/{payment['id']}/capture")
    assert captured.status_code == 200, captured.text

    async with SessionLocal() as session:
        drift = await reconcile(session)

    drifted_ids = {row.account_id for row in drift}
    assert uuid.UUID(payer["id"]) not in drifted_ids
    assert uuid.UUID(payee["id"]) not in drifted_ids


async def test_reconciliation_detects_deliberately_injected_drift(client, payer_payee):
    """
    The required negative case: drift a balance the way a real bug would —
    write balances.balance_minor directly, bypassing ledger.post() entirely
    — and confirm reconcile() catches exactly that account with the right
    numbers, not just "something is wrong somewhere."
    """
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=5_000)
    captured = await client.post(f"/payments/{payment['id']}/capture")
    assert captured.status_code == 200, captured.text

    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                update(Balance)
                .where(Balance.account_id == uuid.UUID(payee["id"]))
                .values(balance_minor=Balance.balance_minor + 999)
            )

    async with SessionLocal() as session:
        drift = await reconcile(session)

    row = next(r for r in drift if r.account_id == uuid.UUID(payee["id"]))
    # recorded is now 999 too HIGH; derived (the ledger) never moved.
    assert row.diff == -999
    # Plain int, not Decimal: Postgres's SUM(bigint) returns NUMERIC, and
    # without an explicit cast `derived` silently becomes a Decimal — which
    # passes `==` comparisons fine but breaks scripts/reconcile.py's `:+d`
    # format string. Locks in the cast in reconciliation.py's query.
    assert type(row.derived) is int
    assert type(row.diff) is int

    # The payer's own balance was never touched and must not show up.
    drifted_ids = {r.account_id for r in drift}
    assert uuid.UUID(payer["id"]) not in drifted_ids


async def test_reconciliation_reports_no_false_positive_under_concurrent_capture(
    client, payer_payee
):
    """
    reconcile() racing a real, concurrent, committing capture — not a
    sequential before/after check. Postgres executes reconcile()'s single
    query against one snapshot regardless of isolation level, so the capture
    is reflected either fully or not at all; this is what proves that, not
    just asserts it.
    """
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=5_000)

    async def do_capture():
        return await client.post(f"/payments/{payment['id']}/capture")

    async def do_reconcile():
        async with SessionLocal() as session:
            return await reconcile(session)

    capture_resp, drift = await asyncio.gather(do_capture(), do_reconcile())

    assert capture_resp.status_code == 200, capture_resp.text
    drifted_ids = {row.account_id for row in drift}
    assert uuid.UUID(payer["id"]) not in drifted_ids
    assert uuid.UUID(payee["id"]) not in drifted_ids

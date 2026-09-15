"""
Reconciliation: re-derive every account's balance from the ledger and report
where the cached balances.balance_minor row disagrees. See the Balance
model's docstring for why that cache exists in the first place — this is the
job that makes trusting it safe, by checking it against the truth
(SUM(ledger_entries.amount_minor)) rather than assuming it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import BigInteger, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Balance, LedgerEntry


@dataclass(frozen=True)
class DriftRow:
    account_id: uuid.UUID
    recorded: int
    derived: int

    @property
    def diff(self) -> int:
        """Positive: the ledger has more than the cache thinks. Negative: less."""
        return self.derived - self.recorded


def _find_drift(rows: Iterable[tuple[uuid.UUID, int, int]]) -> list[DriftRow]:
    """
    Pure comparison over already-fetched (account_id, recorded, derived)
    triples. Split out of reconcile() so the actual "what counts as drift"
    rule is testable without a database — the same reason
    ledger.validate_postings() is a standalone function instead of code
    inlined into post().
    """
    return [DriftRow(a, r, d) for a, r, d in rows if r != d]


async def reconcile(session: AsyncSession) -> list[DriftRow]:
    """
    One query, not one query per account: Postgres executes a single
    statement against a single snapshot regardless of isolation level, so a
    payment that commits concurrently is reflected here either fully or not
    at all — never half-applied mid-transaction.

    LEFT JOIN, not JOIN: an account with zero ledger entries still has a
    balances row (created at account-creation time) and must still be
    checked — COALESCE(..., 0) is what makes "no entries" compare correctly
    against a balance that should also be 0.

    CAST(..., BigInteger): Postgres's SUM(bigint) returns NUMERIC, not
    bigint, to avoid silently overflowing on a huge sum — so without the
    cast, `derived` comes back as a Decimal even though every column
    involved is a BIGINT minor-units column. ADR 0004 rejected NUMERIC
    project-wide specifically to avoid this class of surprise; the cast is
    what keeps that promise here too.
    """
    rows = (
        await session.execute(
            select(
                Balance.account_id,
                Balance.balance_minor,
                cast(func.coalesce(func.sum(LedgerEntry.amount_minor), 0), BigInteger),
            )
            .select_from(Balance)
            .outerjoin(LedgerEntry, LedgerEntry.account_id == Balance.account_id)
            .group_by(Balance.account_id, Balance.balance_minor)
        )
    ).all()

    return _find_drift(rows)

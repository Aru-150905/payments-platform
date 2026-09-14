"""
The double-entry ledger. Everything that moves money goes through post().
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, Balance, LedgerEntry, LedgerTransaction


class LedgerError(Exception):
    pass


class UnbalancedTransaction(LedgerError):
    pass


class InsufficientFunds(LedgerError):
    def __init__(self, account_id: uuid.UUID, available: int, requested: int):
        self.account_id = account_id
        super().__init__(
            f"account {account_id} has {available} minor units, needs {requested}"
        )


@dataclass(frozen=True)
class Posting:
    """One line of a transaction. Positive = debit, negative = credit."""
    account_id: uuid.UUID
    amount_minor: int


def validate_postings(postings: list[Posting]) -> dict[uuid.UUID, int]:
    """
    Check the double-entry invariants and collapse duplicate accounts.

    Pulled out of post() as a pure function on purpose: it holds the rules that
    most need testing, and it can now be tested without a database, a container
    or a running Postgres. Anything that needs infrastructure to test tends not
    to get tested.

    Returns {account_id: net_delta}, so each account is locked exactly once
    even if the caller passed several postings against it.
    """
    if len(postings) < 2:
        raise UnbalancedTransaction("a transaction needs at least two postings")

    if any(p.amount_minor == 0 for p in postings):
        raise UnbalancedTransaction("zero-amount posting")

    total = sum(p.amount_minor for p in postings)
    if total != 0:
        # The defining invariant of double-entry. Money is never created or
        # destroyed, only moved. If this ever fails the bug is upstream, and
        # you want to know now rather than during reconciliation three weeks
        # later when the audit trail is 40 million rows deep.
        raise UnbalancedTransaction(f"postings sum to {total}, expected 0")

    merged: dict[uuid.UUID, int] = {}
    for p in postings:
        merged[p.account_id] = merged.get(p.account_id, 0) + p.amount_minor

    # A posting pair that nets to zero on one account is dropped — locking and
    # writing an entry for a no-op movement is noise in the ledger.
    merged = {k: v for k, v in merged.items() if v != 0}

    if len(merged) < 2:
        raise UnbalancedTransaction("transaction affects fewer than two accounts")

    return merged


async def post(
    session: AsyncSession,
    *,
    kind: str,
    idempotency_key: str,
    postings: list[Posting],
) -> uuid.UUID:
    """
    Write one balanced transaction. Returns the transaction id.

    Caller owns the outer transaction — this function never commits. That is
    deliberate: the whole point of the outbox is that the ledger write, the
    payment status change and the event row all commit together or not at all.
    A commit() hidden in here would break that guarantee for every caller.
    """

    # --- 1. Validate before touching anything -----------------------------
    merged = validate_postings(postings)

    # --- 2. Lock the accounts, in a deterministic order -------------------
    #
    # sorted() is not cosmetic. Transaction A locks account X then Y while
    # transaction B locks Y then X, and both wait forever — a deadlock.
    # Postgres detects it and kills one at random, so under load you get
    # unexplained 500s. Locking in a globally agreed order (here: UUID sort)
    # makes the cycle impossible to form.
    account_ids = sorted(merged.keys(), key=str)

    rows = (
        await session.execute(
            select(Account, Balance)
            .join(Balance, Balance.account_id == Account.id)
            .where(Account.id.in_(account_ids))
            .order_by(Account.id)
            .with_for_update()          # SELECT ... FOR UPDATE: real row locks
        )
    ).all()

    if len(rows) != len(account_ids):
        raise LedgerError("one or more accounts do not exist")

    by_id = {acc.id: (acc, bal) for acc, bal in rows}

    # --- 3. Currency and funds checks -------------------------------------
    currencies = {by_id[a][0].currency for a in account_ids}
    if len(currencies) != 1:
        # Cross-currency movement is a *pair* of transactions plus an FX
        # position account, never one transaction that silently sums INR and
        # USD into a meaningless zero.
        raise LedgerError(f"mixed currencies in one transaction: {currencies}")
    currency = currencies.pop()

    for account_id, delta in merged.items():
        account, balance = by_id[account_id]
        new_balance = balance.balance_minor + delta
        if new_balance < 0 and not account.allow_negative:
            raise InsufficientFunds(account_id, balance.balance_minor, -delta)

    # --- 4. Write ----------------------------------------------------------
    tx = LedgerTransaction(kind=kind, idempotency_key=idempotency_key)
    session.add(tx)

    try:
        # Force the INSERT now so the unique constraint on idempotency_key
        # fires here, before we have written entries and moved balances.
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise

    for account_id, delta in merged.items():
        session.add(
            LedgerEntry(
                transaction_id=tx.id,
                account_id=account_id,
                amount_minor=delta,
                currency=currency,
            )
        )
        by_id[account_id][1].balance_minor += delta

    await session.flush()
    return tx.id


async def get_balance(session: AsyncSession, account_id: uuid.UUID) -> int:
    row = await session.get(Balance, account_id)
    if row is None:
        raise LedgerError(f"no balance row for account {account_id}")
    return row.balance_minor

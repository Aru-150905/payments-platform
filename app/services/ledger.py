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
    Check the STRUCTURAL double-entry invariants and collapse duplicate
    accounts: enough postings, no zero-amount lines, and a whole-transaction
    sum to zero as a cheap, necessary-but-not-sufficient pre-database sanity
    check. Pulled out of post() as a pure function on purpose: it holds the
    rules that most need testing, and it can now be tested without a
    database, a container or a running Postgres. Anything that needs
    infrastructure to test tends not to get tested.

    "Necessary but not sufficient" since M6: a transaction can span more
    than one currency (a trade's cash leg and position leg — see ADR 0008
    Decision 4), and postings summing to zero as one undifferentiated total
    does NOT mean each currency nets to zero on its own — see
    validate_currency_balance() below, which is the check that actually
    enforces that, once each account's currency is known.

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


def validate_currency_balance(
    merged: dict[uuid.UUID, int], currencies: dict[uuid.UUID, str]
) -> None:
    """
    The SUFFICIENT check validate_postings() can't do on its own: group each
    account's net delta by its currency (or, for a position account,
    instrument symbol — see ADR 0008, `currency` is reused as "unit of
    account"), and require every group to net to zero independently.

    Also pure, also pulled out on purpose, for the same testability reason
    as validate_postings() — this is the exact rule that makes a trade's
    cash leg and position leg a valid single transaction, and it deserves
    its own direct tests rather than only being exercised through a live
    ledger.post() call against a database.

    Before M6 this was "exactly one currency across the whole transaction,
    full stop" (see ledger.post()'s history). That flat rule can't express a
    trade settlement at all, so it generalizes to "one currency's postings
    still have to net to zero — there just might be more than one currency
    present." A single-currency transaction satisfies this identically to
    the old rule, because with one currency, "net to zero within the
    currency" and "net to zero across the transaction" are the same
    statement.
    """
    by_currency: dict[str, int] = {}
    for account_id, delta in merged.items():
        currency = currencies[account_id]
        by_currency[currency] = by_currency.get(currency, 0) + delta

    unbalanced = {currency: total for currency, total in by_currency.items() if total != 0}
    if unbalanced:
        raise UnbalancedTransaction(
            f"postings do not net to zero within each currency/instrument: {unbalanced}"
        )


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

    # --- 3. Per-currency balance and funds checks --------------------------
    # See validate_currency_balance()'s docstring and ADR 0008 Decision 4:
    # this generalizes "exactly one currency" to "each currency present
    # nets to zero on its own" — a trade's cash leg and position leg are two
    # different units of account settling in the SAME transaction.
    validate_currency_balance(merged, {a: by_id[a][0].currency for a in account_ids})

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
                # Each entry carries ITS OWN account's currency, not one
                # shared value — the whole point of Decision 4 is that a
                # single transaction can now carry entries in more than one.
                currency=by_id[account_id][0].currency,
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

"""
Re-derive every account's balance from SUM(ledger_entries) and report drift.

`make reconcile`, or `python -m scripts.reconcile`. Exits non-zero on drift
so it can be wired to an alert — a cron job checking the exit code, a CI
step, a health check that pages someone.
"""

import asyncio
import sys

from app.db.base import SessionLocal
from app.services.reconciliation import reconcile


async def main() -> int:
    async with SessionLocal() as session:
        drift = await reconcile(session)

    if not drift:
        print("reconciliation clean: every balance matches SUM(ledger_entries)")
        return 0

    print(f"DRIFT DETECTED in {len(drift)} account(s):")
    for row in drift:
        print(
            f"  account={row.account_id} recorded={row.recorded} "
            f"derived={row.derived} diff={row.diff:+d}"
        )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

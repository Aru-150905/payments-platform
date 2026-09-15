"""
Purge processed_events rows older than the retention window.

`make retention-purge`, or `python -m scripts.retention`. Safe to run on a
schedule (cron, a periodic job) — a row already purged just isn't matched
again, so re-running costs one query and deletes nothing.
"""

import asyncio

from app.db.base import SessionLocal
from app.services.retention import purge_processed_events


async def main() -> None:
    async with SessionLocal() as session:
        async with session.begin():
            deleted = await purge_processed_events(session)
    print(f"purged {deleted} processed_events row(s) past the retention window")


if __name__ == "__main__":
    asyncio.run(main())

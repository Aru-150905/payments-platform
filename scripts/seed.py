"""
Seed the platform's own accounts.

`make seed` after migrating. The clearing account is infrastructure, not a
customer, so it is created here rather than by an API call.
"""
import asyncio

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Account, AccountType, Balance

CURRENCIES = ["INR"]


async def main() -> None:
    async with SessionLocal() as session:
        async with session.begin():
            for currency in CURRENCIES:
                exists = (await session.execute(
                    select(Account).where(
                        Account.owner_id == "platform",
                        Account.name == "clearing",
                        Account.currency == currency,
                    )
                )).scalar_one_or_none()
                if exists:
                    continue

                clearing = Account(
                    owner_id="platform", name="clearing",
                    account_type=AccountType.LIABILITY, currency=currency,
                    # Clearing must be allowed to go negative during settlement
                    # sequencing; a customer wallet must not.
                    allow_negative=True,
                )
                session.add(clearing)
                await session.flush()
                session.add(Balance(account_id=clearing.id, currency=currency))
                print(f"created clearing account {clearing.id} ({currency})")


if __name__ == "__main__":
    asyncio.run(main())

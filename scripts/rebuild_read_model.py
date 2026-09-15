"""
Truncate read_model_payments and replay payments.payment.v1 from offset 0.

`make rebuild-read-model`, or `python -m scripts.rebuild_read_model`.

Do NOT run this while `make worker` is running against the same stack — see
app/events/consumer.py's rebuild() docstring and ADR 0006's consequences.
"""

import asyncio

from app.events.consumer import rebuild


async def main() -> None:
    count = await rebuild()
    print(f"rebuilt read model from {count} event(s)")


if __name__ == "__main__":
    asyncio.run(main())

"""
Retention for processed_events — the consumer dedup table CLAUDE.md invariant
#8 requires ("Consumers dedup on event_id in the same transaction as their
side effects"). This module is what keeps that table from growing forever.

The relationship this module exists to get right:

    processed_events_retention_hours >= kafka_topic_retention_hours

A processed_events row only needs to survive as long as Kafka could still
redeliver the event it guards against. Kafka can only redeliver a message
while it still exists in the topic; once retention.ms expires it, that
offset is gone for good and nothing will ever ask this consumer group to see
it again. So the dedup row is safe to delete once it's older than the
topic's own retention — with margin, because "exactly as long as" leaves no
room for a slow retention-purge job or clock skew to land on the wrong side
of the line.

What happens if the inequality is violated — dedup rows purged BEFORE the
topic would have stopped redelivering them — and Kafka does then redeliver:
the purged row is gone, so app/events/consumer.py's IntegrityError-based
dedup in handle() doesn't fire, and the event is processed again as if new.
For THIS consumer, that's harmless: app/services/read_model.py's projection
is a pure upsert-to-current-values (ADR 0006), so reprocessing just rewrites
the same row. A future consumer with a non-idempotent side effect (calling an
external payment gateway, sending a notification) would NOT be safe under
the same conditions — which is the actual reason for the margin below,
not just defensiveness for its own sake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import ProcessedEvent


def validate_retention_window(*, dedup_hours: int, topic_hours: int) -> None:
    """
    The inequality this whole module rests on, as code instead of only prose
    — called at import time against the real settings, and callable directly
    by a test with arbitrary values.
    """
    if dedup_hours < topic_hours:
        raise ValueError(
            f"processed_events_retention_hours ({dedup_hours}h) must be >= "
            f"kafka_topic_retention_hours ({topic_hours}h), or a dedup record "
            "can expire before Kafka stops redelivering the event it guards. "
            "See this module's docstring."
        )


validate_retention_window(
    dedup_hours=settings.processed_events_retention_hours,
    topic_hours=settings.kafka_topic_retention_hours,
)


def retention_cutoff(*, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now - timedelta(hours=settings.processed_events_retention_hours)


async def purge_processed_events(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete dedup rows older than the retention window. Returns rows deleted."""
    cutoff = retention_cutoff(now=now)
    result = await session.execute(
        delete(ProcessedEvent).where(ProcessedEvent.processed_at < cutoff)
    )
    return result.rowcount or 0

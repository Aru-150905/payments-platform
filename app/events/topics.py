"""
Topic names and the event envelope.

Two rules that will save you later:

1. Topic names are a public contract. Once a consumer subscribes, renaming the
   topic breaks it. Name them <domain>.<entity>.<version>, never "events".

2. Every message carries the same envelope. Consumers must be able to read
   `event_type` and `event_id` WITHOUT knowing the payload schema, otherwise
   you cannot route, log, or deduplicate generically.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

# --- topics -----------------------------------------------------------------

PAYMENT_EVENTS = "payments.payment.v1"
DEAD_LETTER = "payments.dlq.v1"

ALL_TOPICS = [PAYMENT_EVENTS, DEAD_LETTER]


# --- envelope ---------------------------------------------------------------

class EventEnvelope(BaseModel):
    # Unique per message. This is what makes consumers idempotent: a consumer
    # records event_ids it has already processed and drops repeats.
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # e.g. "payment.authorized". Consumers switch on this.
    event_type: str

    # The business key. Kafka guarantees ordering *within a partition*, and the
    # key decides the partition. Same aggregate_id -> same partition -> events
    # for one payment are always delivered in order.
    aggregate_id: str

    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )

    # Schema version of `payload` only, so you can evolve payloads without
    # cutting a new topic every time.
    version: int = 1

    payload: dict[str, Any] = Field(default_factory=dict)

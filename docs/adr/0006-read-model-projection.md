# ADR 0006 — Read model projected from the event stream

**Status:** accepted · **Date:** 2026-09

## Context

`GET /payments/{id}` today reads straight from the `payments` table — the
same table `capture()` and `void()` lock with `SELECT ... FOR UPDATE` on
every write. That's fine at this project's traffic, and it is exactly what
M4 asks to move away from: a read model built by projecting
`payments.payment.v1`, in its own consumer group, rebuildable from offset 0.

The question this ADR answers is *why*, given that querying `payments`
directly is simpler, has zero staleness, and requires no new moving parts.

## Decision

A new table, `read_model_payments`, is written **only** by a Kafka consumer
(`app/events/consumer.py`) projecting `payment.authorized` /
`payment.captured` / `payment.voided` events. Nothing else writes to it — not
the API, not a trigger, not a scheduled job. It has its own consumer group
(`read_model_consumer_group`, distinct from any other group this system ever
adds), so resetting *its* offsets and replaying never touches another
consumer's position in the same topic.

## Why project from the stream instead of querying the write tables

1. **It proves the event log is a source of truth, not just an audit
   trail.** If `read_model_payments` can be dropped and rebuilt byte-for-byte
   from `payments.payment.v1` alone, that is a real, checked claim about the
   system — not a comment asserting it. A view or a scheduled
   `INSERT ... SELECT FROM payments` doesn't test this: it reads the write
   table, so it would keep working even if the outbox silently stopped
   emitting events. Projecting from Kafka means the read model's correctness
   depends on the exact pipeline (outbox → relay → Kafka → consumer) that
   every other future consumer will also depend on. Bugs in that pipeline
   show up here first, on a table nothing else depends on for correctness.

2. **Read-side and write-side schemas decouple.** `payments` is shaped for
   the state machine and the row lock `capture()`/`void()` need
   (`FOR UPDATE`, `version` for optimistic concurrency). A read model shaped
   for queries — denormalized, no lock semantics, free to add columns
   derived from *future* event types without touching the write path or its
   migrations — can diverge from that shape without a debate about whose
   needs win.

3. **Read load never contends with the write path.** Today's scale doesn't
   make this bite, but the mechanism is the one that would matter if it did:
   a read-heavy endpoint hammering `read_model_payments` cannot block or slow
   down a `SELECT ... FOR UPDATE` on `payments`, because they're different
   tables with no shared locks.

## The staleness cost this buys

The read model lags the write path by however long it takes an event to
cross outbox → relay → Kafka → consumer. In this dev stack that's bounded by
the relay's `IDLE_SLEEP_S` (500ms) plus the consumer's poll interval
(~1s) — sub-second in the common case, and **unbounded** if the consumer is
down, since nothing back-pressures the write path to wait for it.

Concretely: `POST /payments/{id}/capture` returns 200 with `status:
"captured"` from the write table the instant it commits. A client reading
`read_model_payments` for that same payment in the next moment can still see
`status: "authorized"`. Any caller of the read model has to accept "correct
as of some recent point," not "correct as of now" — which is exactly why
`payments`, not `read_model_payments`, stays the source of truth for the
state machine and stays what `capture()`/`void()` lock and read.

## Alternatives rejected

- **Query `payments` directly for reads.** Simpler, zero staleness, no new
  table or consumer. Rejected for this milestone specifically because it
  proves nothing about the streaming pipeline — the whole point of M4 per
  the roadmap. Legitimate choice for an endpoint that genuinely needs
  read-your-writes consistency (which is why `GET /payments/{id}` isn't
  being rewired to the read model here).
- **A Postgres materialized view, `REFRESH`ed on a timer.** No Kafka
  round-trip to get wrong, and standard SQL. Rejected for the same reason as
  above (doesn't touch the event pipeline at all), plus a full refresh
  doesn't scale the way an incremental per-event projection does once the
  read model has real volume.
- **A trigger-maintained side table, updated synchronously in the same
  transaction as the write.** Zero staleness, no consumer needed. Rejected
  because it couples the read model's write cost to every payment
  transaction's latency and lock footprint — the opposite of what a read
  model is for — and because triggers hide logic outside the codebase in a
  way `git blame` and `grep` can't find.

## Consequences

- `read_model_payments` can be truncated and rebuilt at any time with no
  data loss, because it holds nothing that isn't re-derivable from the topic.
  That's the whole feature `scripts/rebuild_read_model.py` exists to exploit.
- Rebuild must not run at the same time as the live consumer. Kafka's
  consumer-group protocol would just split the topic's partitions between
  the two processes — the live consumer would keep committing progress on
  whatever partitions it still owns while rebuild's `TRUNCATE` has already
  wiped the table out from under it. There is no lock enforcing this; it's
  an operational rule, documented at the top of the rebuild script.
- The projection must stay pure-by-value (each event's payload fully
  determines the row it writes — an upsert, not an increment or an append).
  That's what makes re-processing an event safe if its dedup record in
  `processed_events` is ever lost before Kafka stops redelivering it — see
  the retention-window justification in `app/services/retention.py`. A
  projection that accumulated state instead (a running counter, say) would
  double-count on replay and this ADR's "rebuild is free" claim would be
  false.

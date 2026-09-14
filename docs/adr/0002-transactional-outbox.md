# ADR 0002 — Transactional outbox for event publication

**Status:** accepted · **Date:** 2026-09

## Context

A payment changes rows in Postgres *and* must emit an event to Kafka. There is
no distributed transaction across the two.

## Decision

The API never publishes to Kafka. It inserts a row into `outbox_events` in the
same database transaction as the business change. A separate relay process
polls unpublished rows, publishes them, and marks them published.

## Alternatives rejected

- **Commit to Postgres, then publish.** A crash in the gap leaves a payment
  with no event. Silent, permanent, and undetectable after the fact — the worst
  possible failure shape.
- **Publish, then commit.** A rolled-back transaction has already announced a
  payment that never happened. Downstream consumers act on a fiction.
- **Two-phase commit.** Kafka has no XA support worth using, and 2PC couples
  availability of the two systems together.
- **Kafka transactions / exactly-once semantics.** Real, but scoped to
  Kafka-to-Kafka flows. They do not span a Postgres write.
- **Debezium CDC on the WAL.** A legitimate and arguably better production
  answer: no polling, no relay to operate. Rejected here only because running
  Debezium + Connect obscures the mechanism this project exists to demonstrate.
  Worth revisiting if relay lag becomes a real problem.

## Consequences

- Publication is **at-least-once**: the relay can crash after publishing and
  before marking the row, so an event may be published twice.
- Therefore every consumer must deduplicate on `event_id`. This is enforced by
  the `processed_events` table, written in the same transaction as the
  consumer's side effects.
- Added latency: currently up to `IDLE_SLEEP_S` (500 ms). Upgrading the relay to
  `LISTEN/NOTIFY` removes almost all of it.

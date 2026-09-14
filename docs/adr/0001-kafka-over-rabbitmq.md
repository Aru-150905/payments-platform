# ADR 0001 — Kafka instead of RabbitMQ/SQS

**Status:** accepted · **Date:** 2026-09

## Context

The system needs asynchronous processing of payment events: settlement,
notifications, a read model, and later a risk and matching pipeline.

## Decision

Use Kafka as the event backbone.

## Why not RabbitMQ or SQS

They are queues: a message is delivered, acknowledged, and deleted. That is a
good fit for work dispatch and a bad fit here, because this system needs three
things a queue structurally cannot give:

1. **Multiple independent readers of the same stream.** A settlement consumer,
   a read-model projector and a risk engine must each see every event. With a
   queue, one consumer's ack destroys the message for the others; you end up
   fanning out to N queues and keeping them in sync by hand.
2. **Replay.** Rebuilding a corrupted read model means re-reading history from
   offset 0. A queue has no history to re-read.
3. **Ordering per entity.** Kafka's partition-per-key gives ordered delivery of
   all events for one payment while still parallelising across payments.

## Cost accepted

Kafka is heavier to operate than RabbitMQ, has no per-message TTL or delay
primitive (so retries need an explicit backoff topic — see ADR 0003), and
consumer-group rebalancing is a real operational concept you have to learn.
For a system whose source of truth is an event log, that cost is worth paying.

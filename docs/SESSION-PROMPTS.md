# Session prompts

Paste one of these into Claude Code and let it run. One per session. Read the
diff and the ADR before starting the next.

Every prompt ends the same way on purpose: build it, test it, explain it,
commit it, push it, then stop.

---

## M3 — retries, DLQ, replay

```
Implement milestone 3 from CLAUDE.md.

Requirements:
- Bounded retries with exponential backoff and jitter for consumer handler
  failures. Explain in a comment why jitter matters (thundering herd on
  redelivery).
- A DLQ topic (payments.dlq.v1) that failed messages land on after retries are
  exhausted, carrying the original payload plus failure metadata: attempt
  count, last error, original topic, original offset.
- A replay CLI: read messages from the DLQ and re-publish them to the original
  topic. It must be idempotent — replaying twice must not double-process.
- Integration tests marked @pytest.mark.integration that run against the
  compose stack and prove: a failing handler retries, exhausts, lands in the
  DLQ, and replays successfully.
- A second CI job for integration tests, using service containers.

Before coding, write docs/adr/0005-retries-and-dlq.md. Cover why a DLQ topic
instead of Kafka's lack of a native delay primitive, why bounded rather than
infinite retries, and what you rejected (blocking retry in the consumer loop,
a retry topic per attempt count).

Then: implement, run ruff and pytest, run the end-to-end check from CLAUDE.md,
commit in logical chunks with real messages, push, and stop. Summarise what
landed and what I should look at.
```

---

## M4 — read model and reconciliation

```
Implement milestone 4 from CLAUDE.md.

Requirements:
- A read model built purely by projecting the event stream, in its own consumer
  group so it can be rebuilt from offset 0 without disturbing other consumers.
- A rebuild command that truncates the read model and replays from the start.
- A reconciliation job that re-derives every balance from SUM(ledger_entries)
  and reports any row where balances.balance_minor disagrees. Non-zero exit on
  drift so it can be wired to an alert.
- A retention job for processed_events, with the retention window justified
  against Kafka's own topic retention. Explain the relationship: what happens
  if dedup records expire before Kafka stops redelivering.
- Tests for all of it.

Write docs/adr/0006-read-model-projection.md first. Cover why project from the
stream instead of querying the write tables, and the staleness cost that buys.

Then: implement, verify, commit, push, stop. Summarise.
```

---

## M5 — resilience and observability

```
Implement milestone 5 from CLAUDE.md.

Requirements:
- Redis-backed sliding-window rate limiter as FastAPI middleware. Explain the
  algorithm choice against fixed-window and token bucket.
- A circuit breaker around the Kafka producer in the relay, so a broker outage
  degrades rather than hammers.
- Prometheus metrics: request latency histogram, outbox lag (oldest
  unpublished row age), consumer lag per partition, ledger transaction rate.
  A Grafana dashboard as JSON in docs/.
- k6 load test script hitting POST /payments with unique idempotency keys, plus
  a second scenario hammering the SAME key to prove idempotency under
  concurrency.
- API key auth on write endpoints. Keep it simple; note in the ADR what a real
  system would do instead.

Watch the RAM budget — 8 GB machine. Prometheus and Grafana are two more
containers; put them behind a compose profile that is off by default.

Write docs/adr/0007-observability.md first. Then implement, verify, commit,
push, stop. Summarise — and tell me which metric I should be able to explain
the meaning of.
```

---

## M6 — trading module

```
Implement milestone 6 from CLAUDE.md.

This is the milestone I most need to understand, so bias hard toward
explanation over speed. Smaller scope, better comments.

Requirements:
- instruments table; position accounts on the existing ledger (an account per
  (owner, instrument)), so a trade is a ledger transaction like any other.
- orders table with an explicit order state machine, in the same style as
  app/domain/state_machine.py.
- A price-time-priority matching engine as a PURE function over an order book
  snapshot — no database access inside the matcher, so it is testable without
  infrastructure. This matters; do not shortcut it.
- Limit and market orders only. Partial fills yes. No auctions, no icebergs,
  no stop orders.
- Trades settle into the ledger: cash leg and position leg in one balanced
  transaction.
- Order and trade events on their own topic, keyed so all orders for one
  instrument stay ordered.
- Thorough unit tests on the matcher: price priority, time priority at equal
  price, partial fills, market order sweeping multiple levels, empty book,
  self-match.

Write docs/adr/0008-matching-engine.md first. Cover why the matcher is pure and
in-memory, why Kafka is the durability layer rather than the hot path, and what
a real exchange does differently.

Then implement, verify, commit, push, stop. Summarise.
```

---

## Any session — catch-up mode

Use this when you come back and want to learn rather than build.

```
I want to understand what you built in the last milestone, not add anything.

Walk me through it in this order:
1. Plain English: what problem this milestone solves and where it is used in
   real systems.
2. Intuition: why this approach, why this order of operations, why each new
   data structure and variable exists.
3. Every non-obvious line, with the reasoning and a counterexample showing why
   the obvious alternative breaks.
4. A full trace of one request or one event through the new code, showing every
   intermediate value and what each queue or table contains at each step.
5. Edge cases: empty, single item, concurrent, failure mid-way.
6. Derived time and space complexity where relevant, not just stated.
7. Interview angle: the follow-up questions this invites and the mistakes people
   make here.

Use ASCII diagrams. Do not change any code this session.
```

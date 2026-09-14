# CLAUDE.md

Read at the start of every Claude Code session. These are standing rules for
this repo, not suggestions.

## What this project is

A modular-monolith payments backend on a double-entry ledger, with Kafka as the
event backbone. A trading module (orders + price-time-priority matching) lands
later on the same ledger. Python 3.12, FastAPI, PostgreSQL, Kafka (KRaft),
Redis, Alembic.

It is a learning and portfolio project. The owner must be able to explain every
design decision in an interview. That constraint outranks speed.

## Working agreement

- **One milestone per session.** Do not start the next milestone, even if the
  current one finishes early. Stop and summarise instead.
- **Explain as you go.** Every non-obvious line gets a comment saying *why*,
  not what. Assume the reader is a strong student who has not seen the pattern
  before.
- **Write the ADR before the code**, for any decision with a real alternative.
  Record what you rejected and why. `docs/adr/` has the format.
- **Never silence a check.** If ruff or pytest complains, fix the cause. If a
  rule is genuinely wrong for this codebase (e.g. B008 for FastAPI `Depends`),
  narrow the exemption to the specific case and say why in a comment.
- **Ask before changing an invariant.** The list below is the contract.

## Invariants — do not break these

1. `SUM(ledger_entries.amount_minor)` per `transaction_id` is exactly 0.
2. `balances.balance_minor` equals the sum of that account's entries.
3. Ledger entries are append-only. Corrections are reversing transactions,
   never UPDATE or DELETE.
4. Money is `BIGINT` minor units. No floats, anywhere, ever.
5. Every write endpoint is idempotent, enforced by a unique index — never by an
   application-level "check then insert".
6. Events are published only via the outbox. Service code never calls Kafka
   directly.
7. `ledger.post()` never commits. The caller owns the transaction boundary.
8. Consumers dedup on `event_id` in the same transaction as their side effects.

## Conventions

- Topics: `<domain>.<entity>.v<N>`. Renaming a topic is a breaking change.
- Partition key is always the aggregate id, so per-entity ordering holds.
- Config only via `app/core/config.py`. No literals for hosts, ports or URLs.
- Tests that need no infrastructure go in `tests/`; infrastructure tests are
  marked `@pytest.mark.integration` and excluded from the fast CI job.
- Commit messages: `type: summary`, then a bullet list of what landed.
  One commit per logical change, not one per milestone.

## Verify before every commit

```bash
ruff check .
pytest -q
```

For anything touching the ledger, outbox, or a consumer, also run the
end-to-end path and confirm it works:

```bash
make up && make migrate && make seed
make relay & make worker & make api &
make demo
```

## Roadmap

- **M1** (done) compose stack, event envelope, producer, consumer
- **M2** (done) ledger, payment state machine, idempotency, outbox + relay
- **M3** retries with exponential backoff, DLQ topic, replay CLI, integration
  tests against the compose stack
- **M4** read model projected from the stream, reconciliation job that
  re-derives balances and alarms on drift, `processed_events` retention
- **M5** Redis rate limiter + circuit breaker, k6 load tests, Prometheus +
  Grafana (consumer lag is the metric that matters), auth on endpoints
- **M6** trading module: instruments, orders, order state machine, price-time
  priority matcher, trades settling into the ledger. Keep it simple — limit and
  market orders only, no auctions, no icebergs.
- **M7** AWS deploy (MSK or Redpanda Cloud), IaC, CI/CD, runbook

## Hard constraints

- Dev machine is a MacBook Air M2 with 8 GB RAM. Keep the compose stack light;
  cap JVM heaps; do not add another JVM service without asking.
- Do not add a dependency without saying in the commit message why the standard
  library or an existing dependency is insufficient.
- Do not introduce microservices, Kubernetes, or a service mesh. See ADR 0003.

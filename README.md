# payments-platform

<!-- Replace <you> with your GitHub username once the repo is pushed. -->
![ci](https://github.com/<you>/payments-platform/actions/workflows/ci.yml/badge.svg)

A modular-monolith payments and (later) trading backend built on a shared
double-entry ledger. Python + FastAPI + PostgreSQL + Kafka.

## Status

- **M1** — compose stack (Postgres, Redis, Kafka KRaft, Kafka UI), event envelope, producer, consumer.
- **M2** — ledger core: double-entry schema, payment state machine, idempotency,
  transactional outbox, outbox relay, idempotent consumer, Alembic migrations. **← you are here**
- **M3** — retries with exponential backoff, DLQ topic, replay tool.
- **M4** — read model projected from the event stream, reconciliation job.
- **M5** — Redis rate limiter / circuit breaker / distributed lock, k6 load tests, Prometheus + Grafana.
- **M6** — trading module (order state machine, price-time-priority matcher, trades settling into the ledger).
- **M7** — AWS deploy, IaC, CI/CD, ADRs.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

make up        # infrastructure
make topics    # declare Kafka topics explicitly
make migrate   # alembic upgrade head
make seed      # platform clearing account
```

Three processes, three terminals:

```bash
make api       # FastAPI      :8000
make relay     # outbox -> Kafka
make worker    # Kafka -> read model
```

Then:

```bash
make demo      # end-to-end: accounts, payment, idempotent retry, capture, 409
```

Kafka UI: http://localhost:8080 — watch `payments.payment.v1` fill up.

## Architecture

```
        HTTP
          |
          v
   +--------------+     one DB transaction     +---------------+
   |  FastAPI     |--------------------------->|  PostgreSQL   |
   |  (no Kafka)  |   payment + ledger entries |               |
   +--------------+   + outbox row            |  outbox_events|
                                               +-------+-------+
                                                       |  poll (FOR UPDATE SKIP LOCKED)
                                                       v
                                               +---------------+
                                               |  relay        |
                                               +-------+-------+
                                                       | publish (acks=all, idempotent)
                                                       v
                                               +---------------+
                                               |  Kafka        |
                                               | payments.*.v1 |
                                               +-------+-------+
                                                       | consumer group
                                                       v
                                               +---------------+
                                               |  worker       |
                                               | dedup on      |
                                               | processed_    |
                                               | events        |
                                               +---------------+
```

The API never talks to Kafka. That is the point: a broker outage cannot fail a
write, because the event is already durable in Postgres and the relay will catch
up when Kafka returns.

## Tests

```bash
pytest -q          # 17 unit tests, no database or Kafka required
ruff check .
```

The ledger invariants and the payment state machine are pure functions on
purpose, so the rules that matter most are tested in under a second and run on
every push. Integration tests against the compose stack arrive in M3 as a
separate, slower CI job.

## Design decisions

See [`docs/adr/`](docs/adr/) — Kafka over RabbitMQ, the transactional outbox,
modular monolith over microservices, and integer money.

## Invariants worth stating out loud

1. `SUM(ledger_entries.amount_minor)` over any `transaction_id` is exactly 0.
2. `balances.balance_minor` equals `SUM(ledger_entries.amount_minor)` for that
   account. M4's reconciliation job verifies this and alarms on drift.
3. Ledger entries are append-only. Corrections are reversing transactions.
4. Money is BIGINT minor units. No floats anywhere.
5. Every write endpoint is idempotent, enforced by a unique index, not by an
   application-level "check then insert".

## Known gaps

| Gap | Fixed in |
|---|---|
| Failed events are logged and dropped | M3 |
| Relay polls every 500ms instead of LISTEN/NOTIFY | M3 |
| No schema registry; payloads are free-form JSON | M4 |
| `processed_events` grows forever, no retention job | M4 |
| No auth on any endpoint | M5 |

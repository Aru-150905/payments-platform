# ADR 0005 — Integration tests isolate by unique data, not by DB reset

**Status:** accepted · **Date:** 2026-09

## Context

Two bugs shipped that the 17 unit tests could not catch, because both only
manifest across repeated invocations against state a *previous* call left
behind:

- A repeated `capture`/`void` reached `ledger.post()` before the state-machine
  check and hit `uq_ledger_tx_idempotency`, surfacing as a 500 instead of a
  409 (fixed in `5954ac8`).
- `scripts/demo.sh` reused a fixed idempotency key across runs, so every run
  after the first silently returned the first run's payment (fixed in
  `cb602af`).

Unit tests exercise `validate_postings()` and `assert_can_transition()` as
pure functions — no database, no repeat invocations, no way to hit a unique
constraint. Catching these requires a real Postgres and a real HTTP-level
retry against the same entity. That means integration tests need a strategy
for state between runs: the suite must pass on a fresh database *and* pass
again immediately after, without a manual reset, because a suite that only
works once hides exactly the class of bug it exists to catch.

## Decision

Every integration test generates its own unique accounts (via a per-test
UUID-suffixed `owner_id`) and its own unique idempotency key, instead of
relying on fixture/seed data shared across runs or on the database being
wiped between them. Repetition *within* a test (the same key or the same
payment id hit twice) is deliberate and is the thing under test; repetition
*across* test runs is avoided by never reusing a key. The only shared,
cross-run state a test may depend on is the seeded `platform/clearing`
account, which is looked up by query rather than assumed to have a fixed id.

Assertions compare balances by delta (`before` vs `after` a call), not by
absolute value, since the account persists across runs and its absolute
balance is not test-owned state.

## Alternatives rejected

- **Wrap each test in an outer transaction, roll back at teardown.** The
  standard sync-test pattern, but it doesn't fit here: the app being tested
  owns its own transaction per request (`get_session` opens and commits one
  per call), so a test-level outer transaction would need every route's
  session dependency overridden to share and never commit a single
  connection. That's invasive to `app/db/base.py` for a project whose stated
  point is to demonstrate the transaction-boundary pattern in `get_session`,
  and it would stop the test from observing what a real client observes
  (committed rows visible to the next request).
- **`docker compose down -v` / truncate tables between runs.** Works, but
  turns "run the suite" into a two-step ritual a future session (or CI job)
  will eventually forget, and it fights the project's own idempotency
  invariant instead of exercising it — the bugs above were about *correct*
  behaviour under repetition, not about needing a clean slate.
- **`testcontainers` to spin an ephemeral Postgres per run.** A real option,
  and closer to "no shared state at all." Rejected for now: it's a new
  dependency and a slower suite for a single-developer, single-machine setup
  that already has the compose stack running most of the time; worth
  revisiting if this becomes a multi-contributor or CI-parallel project.

## Consequences

- Running `pytest -m integration` twice in a row against the same running
  stack passes both times — the property this ADR exists to guarantee.
- The database accumulates test rows over time (accounts, payments, ledger
  entries) with no expiry. Acceptable for a dev/demo stack; would need a
  retention policy for a long-lived shared environment, which this project
  does not have.
- A test can never assert "the platform clearing account has balance X" —
  only "capturing this payment moved it by X." This is a *better* test in a
  system with a shared clearing account, since two tests running concurrently
  cannot stomp on each other's absolute-balance assertion either.

## Postscript: this suite found a third bug on first run

`test_concurrent_creation_with_same_key_agrees_on_one_payment` — two real
HTTP requests racing on the same `Idempotency-Key`, not a sequential retry —
failed immediately, and not on the assertion: `create_and_authorize()`'s
loser caught the `IntegrityError` from its own `flush()` and called
`session.rollback()` while still nested inside the caller's `async with
session.begin():` (`routes.py`), which left the *outer* transaction unusable
for the follow-up read. A `session.begin_nested()` SAVEPOINT doesn't fix this
either — an ORM flush failure deactivates the Session's transaction tracking
outright, savepoint or not. The fix (in `app/services/payments.py` and
`app/api/routes.py`) lets the `IntegrityError` propagate through the outer
`session.begin()` for a full, clean rollback — the same shape every other
error from this function already used — and reads the winning payment back
in the route, after the transaction has fully closed.

This is exactly the failure mode this ADR exists to catch: a sequential
retry (the earlier, passing `test_repeated_creation_with_same_key_*` tests)
never reaches the losing branch at all, because the second call's initial
existence check finds the first call's already-committed row and returns
early. Only real concurrency exercises the recovery path — which is why 17
unit tests and a sequential demo script both missed it.

# ADR 0007 — Rate limiting, circuit breaking, metrics, and auth (M5)

**Status:** accepted · **Date:** 2026-09

## Context

M5 adds four things that don't change what the system does, only what happens
when it's under load or partially broken: a rate limiter in front of the API,
a circuit breaker in front of the relay's Kafka producer, Prometheus metrics,
and a simple API key on write endpoints. Each has a real alternative worth
writing down, because "why not the simpler thing?" is the question an
interviewer asks first.

## Decision 1: sliding-window rate limiting, not fixed-window or token bucket

**Fixed window** keeps one counter per key, reset at wall-clock boundaries
(e.g. every 60s). It's O(1) storage and the cheapest thing that could work,
but it has a real hole: nothing stops a client from sending the full limit in
the last second of one window and the full limit again in the first second of
the next. A 60-req/min limit enforced this way lets 120 requests through in
under 2 seconds, right on the boundary. For a payments API sitting in front
of a connection pool and a Kafka producer, that's the exact spike the limiter
exists to prevent — cheapest to build, weakest guarantee.

**Token bucket** refills tokens at a steady rate up to a capacity; each
request spends one. It fixes the boundary problem and its whole point is
letting *legitimate* bursts through up to the bucket size while capping the
long-run average — the right shape for smoothing traffic you're generating
(e.g. your own outbound calls to a third-party API with a known capacity).
That's the wrong shape here: an inbound limiter's job is to protect the DB
pool and the Kafka producer from an unpredictable spike, and burst tolerance
is a liability for that job, not a feature.

**Sliding window** (implemented as a log of request timestamps, here a Redis
sorted set) enforces "no more than N requests in *any* trailing window of
length W" exactly — no boundary to burst across, no allowance for burst by
design. The cost is O(N) storage per key instead of O(1), but N is bounded by
the limit itself (60 timestamps for a 60-req/min limit is nothing), and the
guarantee is the one that's actually easy to explain and easy to trust:
"never more than the limit, full stop," in the same spirit as this project's
other exact invariants (CLAUDE.md's ledger sum, idempotency-by-unique-index).
That's why it's the default here over the other two.

**Boundary rule, made explicit because it's where off-by-one bugs live:** a
request's window is `[now - window_ms, now]`, both ends inclusive. An event
timestamped exactly `now - window_ms` still counts against the limit; an
event one millisecond older has fully exited the window. `app/api/rate_limit.py`
implements this as two pure functions (`window_cutoff`, `count_in_window`)
with no Redis dependency, specifically so the arithmetic can be unit tested
without infrastructure — see Phase 2.

**Why Redis and not an in-process counter:** the API can run as more than one
uvicorn worker/process, and a limiter that only sees its own process's
traffic isn't limiting anything in aggregate. Redis is already a dependency
(`settings.redis_url`) for exactly this kind of shared, low-latency counter.

**Atomicity:** the check ("how many requests in the window?") and the write
("record this one") must happen as one atomic step, or two concurrent
requests can both read "under the limit" and both get admitted, overshooting
it. `app/api/rate_limit.py` does this with a Lua script run via `EVAL`
(prune-then-count-then-maybe-add, all inside Redis's single-threaded command
execution) rather than separate `ZCARD`/`ZADD` round trips from Python.

## Decision 2: a circuit breaker around the relay's Kafka producer

**States:**
- **CLOSED** — normal. Every `publish()` call is attempted; failures increment
  a counter.
- **OPEN** — the breaker trips after `failure_threshold` *consecutive*
  failures. While open, `publish()` raises `CircuitOpenError` immediately —
  no network call is attempted at all.
- **HALF_OPEN** — entered automatically once `recovery_timeout_s` has elapsed
  since the trip. The next `publish()` is let through as a trial. Success
  closes the breaker (failure count resets to zero); failure reopens it and
  restarts the cooldown.

**Why this beats naive retry-with-backoff against a dead broker:** a naive
retry loop still *attempts* a real connection on every try, and against a
genuinely unreachable broker each attempt pays the full TCP/produce timeout
before failing — tens of seconds with `aiokafka`'s defaults. `drain_once()`
in `app/events/relay.py` holds `FOR UPDATE SKIP LOCKED` on a batch of up to
100 outbox rows for the duration of the transaction that calls `publish()`;
a naive retry against a dead broker would hold those row locks (and the
surrounding DB transaction) open for as long as the retries take, which is a
self-inflicted second outage layered on top of the first. A tripped breaker
fails in microseconds — no I/O — so the transaction rolls back almost
immediately and the relay's existing `except Exception: ... sleep(2)` in
`run()` actually paces the retry interval, instead of the retry interval
being "whatever TCP felt like." It also stops hammering a broker that's in
the middle of recovering, which is the textbook reason circuit breakers exist
in the first place (Nygard, *Release It!*): retrying full-force *into* a
struggling dependency is exactly the load that keeps it struggling.

HALF_OPEN is the piece that makes this self-healing instead of a manual
on/off switch someone has to remember to flip back — it's the one state
whose job is to ask "is it back?" without fully reopening the floodgates
until the answer is yes.

**Defaults chosen:** `failure_threshold=5`, `recovery_timeout_s=30`,
one successful trial closes the breaker (`success_threshold=1`). Simplicity
was picked deliberately over e.g. requiring N consecutive half-open successes
— the relay publishes sequentially within one process, so there's no
concurrent trial-request problem a stricter half-open policy would be
guarding against here.

## Decision 3: four metrics, and why consumer lag is the one that matters

1. **`http_requests_total{method,route,status}`** (counter) — request volume
   and error rate, the first thing any HTTP service should expose.
2. **`http_request_duration_seconds{method,route}`** (histogram) — latency,
   so p50/p95/p99 are queryable instead of guessed at.
3. **`kafka_circuit_breaker_state{component}`** (gauge, 0=closed/1=half_open/
   2=open) — direct visibility into the failure-handling path Decision 2
   just added; without it, "is the breaker open right now" is a question
   only the logs can answer.
4. **`kafka_consumer_lag{topic,partition,group}`** (gauge) — offset backlog
   between what's been produced and what a consumer group has committed.

**Why lag is the one that matters most:** `app/main.py`'s readiness probe
deliberately does *not* check Kafka — the comment there explains that the
outbox is what lets the API keep accepting writes while Kafka is down, and a
readiness check that failed on a Kafka outage would throw that benefit away.
That's the right call for availability, but it means the HTTP surface can
report perfectly healthy — 200s, low latency, no errors — while the read-model
consumer is wedged or dead and `read_model_payments` (ADR 0006) drifts stale
for hours with nothing surfacing it. Request count and latency describe the
synchronous HTTP path only, and by this project's own design that path is
decoupled from Kafka's health — so those two metrics *cannot* ever reveal a
Kafka-side problem, no matter how bad it gets. Consumer lag is the metric
that closes that blind spot: it's the earliest signal of either failure mode
that can hide behind a healthy API — a dead consumer (lag climbs and never
recovers) or a stuck/dead producer path (lag stays flat because nothing new
is arriving to fall behind on, distinguishable by cross-checking against
`http_requests_total` on the write endpoints).

## Decision 4: a simple API key now, versus what real auth would be

What's being added: one static secret (`settings.api_key`), sent as the
`X-API-Key` header, compared with `hmac.compare_digest` (constant-time, so a
timing side-channel can't leak the key one byte at a time) and required by
every route in `app/api/routes.py`. It also becomes the rate limiter's bucket
key when present, so limiting is per-caller rather than per-IP.

What it is *not*: there's exactly one valid secret, shared by every caller,
with no expiry and no scoping. Revoking a leaked key means rotating the one
value for everyone at once — every legitimate caller breaks in the same
moment the leaked one does. There's no way to tell two callers apart in logs
or metrics; `X-API-Key: <the value>` on a request proves only "knows the
secret," not "is who they claim to be." A copy of the header value, however
it leaked, is valid forever until manual rotation.

Real production auth would typically be:
- **Per-caller credentials**, not one shared secret — OAuth2 client
  credentials or mTLS client certs, so one caller can be revoked without
  touching anyone else's.
- **Short-lived signed tokens** (JWT/PASETO) issued after authenticating the
  client, rather than a long-lived bearer value — a captured token expires; a
  captured API key doesn't.
- **Scoped permissions** — this caller may `POST /payments` but not
  `POST /accounts`, instead of all-or-nothing.
- **Rotation without downtime** — accepting old-and-new credentials
  simultaneously during a rotation window, which a single hardcoded value
  can't do without a deploy that briefly rejects one side.
- **A real audit trail** — actions attributable to a specific caller identity,
  not just "someone who had the key."

For a single-developer portfolio project with no real external callers, a
static key is the right-sized choice: it demonstrates the endpoint isn't
wide open and gives the rate limiter a real per-caller dimension, without
standing up a token service for an audience of one.

## Alternatives rejected

- **Fixed-window and token-bucket rate limiting** — see Decision 1.
- **Retry-with-backoff alone, no breaker, around the producer** — see
  Decision 2; the failure mode it doesn't fix is retries paying full network
  timeouts while holding the relay's row locks.
- **IP-based rate limiting instead of per-API-key** — simpler (no auth
  dependency needed first), but wrong for this system: every request from
  behind the same NAT or the same docker network shares a budget, and once
  Decision 4 adds a caller identity, throwing it away for the limiter's key
  would be pointless.
- **Checking Kafka in `/health/ready` instead of adding a lag metric** —
  rejected because it's the exact tradeoff `app/main.py`'s existing comment
  already rejected: it would fail API readiness on a Kafka outage the outbox
  is specifically designed to survive.
- **OAuth2/JWT now instead of a static key** — correct for production, wrong
  for this project's actual threat model and audience; see Decision 4.

## Consequences

- Rate limiting and the breaker both need Redis reachable from the API/relay
  processes; `settings.redis_url` already exists for this (M1), no new
  service.
- The rate limiter's atomicity depends on the Lua script being correct — a
  bug there is a bug in the admission decision itself, not just in test
  coverage, so its boundary arithmetic gets unit tests with no real Redis
  (Phase 2).
- The breaker's `failure_threshold`/`recovery_timeout_s` are guesses, not
  measurements — there's no production traffic history to tune them against.
  They're config (`app/core/config.py`), not constants, specifically so
  they can move without a code change once real numbers exist.
- A leaked `api_key` compromises every caller at once (see Decision 4) — the
  static-key tradeoff is accepted here, not resolved.
- `kafka_consumer_lag` requires the consumer process to expose its own
  `/metrics` endpoint (it isn't part of the FastAPI app), which is new
  process-level surface area Prometheus's scrape config has to know about.

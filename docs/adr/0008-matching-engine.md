# ADR 0008 — A pure in-memory matcher, Kafka as durability not hot path, and one changed invariant

**Status:** accepted · **Date:** 2026-09

## Context

M6 adds a trading module: instruments, orders, a price-time-priority
matching engine, and trades that settle into the existing ledger. Three
design questions decide almost everything else in this milestone, so they're
answered here before any code: why the matcher has to be a pure function
with no database inside it; why Kafka publishes what the matcher decided
instead of being part of deciding it; and what a real exchange does that
this project deliberately doesn't. A fourth section documents something this
milestone actually changes about the ledger — CLAUDE.md says to ask before
touching an invariant, and the honest way to "ask" in a written record is to
show the change and the reasoning in the open, not bury it in a diff.

## Decision 1: the matcher is a pure function over a snapshot, with zero I/O

`app/domain/matching.py`'s `match()` takes an `OrderBook` (the resting bids
and asks for one instrument) and one incoming order, and returns the trades
it produces plus the new book state. It never opens a session, never awaits
anything, never imports SQLAlchemy or `aiokafka`. Every dependency it has is
a plain Python dataclass defined in the same file.

**Why this matters enough to insist on, not just prefer:** a matching engine
is the one piece of this whole system where a subtle bug is genuinely hard
to see by reading the code — price-time priority across two sides of a book,
partial fills, a market order sweeping several price levels, are the kind of
logic that *looks* right and is wrong in a case you didn't think to trace by
hand. The only way to be confident it's right is to hit it with dozens of
sharp, specific cases — exactly at a price boundary, exactly at equal
timestamps, an empty book, a self-match — and a function you can call
directly from `pytest` with no setup is one you'll actually run that many
times. `app/services/ledger.py`'s `validate_postings()` already established
this pattern in M2 for the same reason (see its docstring: "anything that
needs infrastructure to test tends not to get tested"). The matcher is the
same idea under harder conditions, because unlike a ledger posting, a match
decision depends on *comparing many orders against each other*, not
validating one payload — there's a combinatorial space of orderings a
database round-trip would make expensive to explore in a test suite you're
supposed to run on every save.

**A concrete cost this buys:** `tests/test_matching.py` runs its whole suite
— price priority, time priority, partial fills, a market order crossing
three price levels, an empty book, a self-match — in well under a second,
with no Postgres, no Docker, nothing running. That suite runs on every
`pytest -q`, which is the fast, infra-free CI job (`pyproject.toml`'s
`addopts`). If matching needed a database, those tests would either move to
the slow `-m integration` job (run far less often) or just not get written
with this much coverage, because nobody hand-writes forty database-backed
fixtures for one function.

## Decision 2: Kafka is the durability/notification layer, not where matching happens

The matching decision itself — which resting orders an incoming order fills
against, at what price, for how much — happens synchronously, in-process,
inside the database transaction that placed the order (`app/services/
trading.py`'s `place_order()`). Kafka only enters after that decision is
already final: `order.placed` and `trade.executed` events go through the
SAME outbox-and-relay pipeline every other event in this system uses (ADR
0002), published *after* the matching transaction commits.

**Why not let Kafka mediate the match itself** (e.g., publish incoming
orders to a topic and have a consumer do the matching asynchronously): that
would make "was my order filled" an eventually-consistent question — exactly
the staleness ADR 0006 accepts for the read model, where a client is told
up front to expect "correct as of some recent point." A trading order is the
opposite case: the placer needs to know, in the HTTP response to placing it,
whether it just executed, partially executed, or is resting — the same
reason `POST /payments/{id}/capture` returns the outcome synchronously
instead of "we'll get back to you." Matching has to be on the synchronous
path for the same reason ledger posting already is.

**Why Kafka still matters here at all, then:** durability and fan-out for
everyone who ISN'T the placer. A market-data feed, an analytics pipeline, or
a future read model (the same shape as ADR 0006's) all want to know "a trade
happened" without querying the trading tables directly or coupling to their
schema — and they want it to survive this process crashing between deciding
and telling them, which is exactly the guarantee the outbox already provides
for payments. This is a repeat of ADR 0002's outbox reasoning, not a new
argument: the write to Postgres (`orders`, `trades`, ledger entries) is the
system of record, and Kafka is the durable broadcast of what that write
decided, published after the fact by a relay that can crash and retry freely
because consumers dedup on `event_id` (invariant #8).

## Decision 3: what a real exchange does differently

Worth being explicit about, since the gap between this project and a real
venue is instructive, not embarrassing:

- **A dedicated, single-threaded, in-memory matching engine per instrument**,
  not a database row lock. This project locks every resting order for an
  instrument with `SELECT ... FOR UPDATE` before matching (`app/services/
  trading.py`), which serializes concurrent orders for the same instrument
  correctly but is orders of magnitude slower than what it's standing in
  for: a real venue keeps the live book for one instrument in the memory of
  one thread that owns it exclusively, so "lock" is just "this is the only
  thread that ever touches this book" — no lock, no wait, no context switch.
  Matching latency at a real exchange is measured in microseconds; a
  Postgres row lock plus a network round trip is measured in milliseconds.
  That's a 1000x gap, and it's the single biggest thing this project doesn't
  attempt to close.
- **Funds and shares are reserved at order entry, not checked at
  settlement.** This project's `ledger.post()` only discovers "insufficient
  funds" or "insufficient shares to sell" when a trade tries to settle
  (Decision 4 explains why that check still works with the ledger it has).
  A real venue's risk/margin engine holds the funds or shares the moment an
  order is *accepted*, so an order that will fail on settlement is rejected
  before it ever rests in the book or matches anyone else's order. This
  project accepts a narrow gap here: a resting order's owner could, in
  theory, spend the funds an already-resting order depends on via some
  other action before that order matches. See Decision 4's consequences.
- **A central counterparty (a clearing house), not buyer-and-seller-direct
  settlement.** This project's trade settlement moves cash and shares
  directly between the two trading accounts in one ledger transaction. Real
  securities markets interpose a clearing house between every buyer and
  seller (via novation) specifically so that if one side later fails to
  deliver, the CCP — not the other trader — absorbs the loss. That's an
  entire second ledger and legal structure this project doesn't build.
- **Self-trade prevention.** This matcher is deliberately ownership-blind —
  it will match an incoming order against the same owner's own resting
  order and produce a real fill (see `tests/test_matching.py`'s self-match
  test, and Decision 4's note on what settling one actually does). Real
  venues detect this at match time and skip the self-crossing order,
  because an unintentional wash trade can be a regulatory problem, not just
  a bookkeeping curiosity. Adding that here would mean the matcher needs to
  know who owns which order, coupling price-time logic to an identity
  concern it doesn't otherwise need — deliberately out of scope; noted, not
  silently skipped.
- **More order types, time-in-force, and market state.** No stop orders, no
  iceberg orders, no auctions (opening/closing crosses), no circuit
  breakers, no limit-up/limit-down bands — all explicitly out of scope per
  the roadmap, and each is a real thing a production venue does to manage
  volatility and information leakage that this project's "limit and market
  orders only" scope skips entirely.
- **A binary wire protocol and colocation**, not JSON over an HTTP API. Not
  worth belaboring — this project's transport is a deliberate, enormous
  latency trade against a problem (interview-explainable double-entry
  trading on a portfolio project) that was never trying to compete on speed.

## Decision 4: generalizing the ledger's single-currency invariant

CLAUDE.md invariant #1 is `SUM(ledger_entries.amount_minor)` per
`transaction_id` is exactly 0, and `ledger.post()` additionally required
every account in one transaction to share **one** currency
(`app/services/ledger.py`, pre-M6: `if len(currencies) != 1: raise
LedgerError(...)`). Settling a trade needs a cash leg (buyer's INR account,
seller's INR account) and a position leg (buyer's shares, seller's shares)
in the *same* atomic transaction — "one balanced transaction," per the
roadmap — and a cash amount and a share count are not the same unit. A flat
single-currency rule makes that transaction impossible to express, so this
milestone changes it.

**The change:** `ledger.py` now checks that postings net to zero *within
each currency*, not across the whole transaction as one undifferentiated
sum. `validate_postings()` (unchanged) still does the cheap structural
checks — at least two postings, no zero-amount postings, and a **necessary**
whole-transaction sum-to-zero as a fast pre-database sanity check. A new
pure function, `validate_currency_balance()`, does the **sufficient**
check once each account's currency is known (post-lock, inside `post()`):
group the net deltas by currency, and every group must sum to zero on its
own. This is a strict generalization, not a loosening: a single-currency
transaction (every payment this project has ever posted) still has to
satisfy the exact same rule it always did, because with one currency the
per-currency check and the whole-transaction check are the same check. Only
a transaction spanning more than one currency behaves differently — and
before M6, no such transaction could exist at all.

**Reusing `currency` as "unit of account":** rather than add a parallel
`instrument_id` concept to `accounts`/`balances`/`ledger_entries`, a
position account's `currency` column holds the instrument's symbol (e.g.
`AAPL`) instead of an ISO code (e.g. `INR`). The column is genuinely just a
label the per-currency balance check groups by — nothing in `ledger.post()`
ever validates it against a real currency list. This means "an account per
(owner, instrument)" (the roadmap's phrase) is not a new kind of account at
all: it's an ordinary `accounts` row, with an ordinary `balances` row, that
happens to be denominated in "shares of AAPL" instead of "rupees" — and it
flows through `ledger.post()`, `InsufficientFunds`, and every other piece of
M2's machinery completely unchanged. That reuse is the entire point of the
roadmap's "so a trade is a ledger transaction like any other." The
`currency` column widens from `VARCHAR(3)` to `VARCHAR(16)` and its CHECK
constraint relaxes from `^[A-Z]{3}$` to `^[A-Z0-9]{1,16}$` to fit real ticker
symbols; see the M6 migration.

**No shorting, by reusing an existing mechanism, not a new one:** a position
account is created with `allow_negative=False`, exactly like a customer
payments wallet. Selling shares you don't hold fails with the *exact same*
`InsufficientFunds` a customer overdrawing their wallet gets — no new
concept, no new check, just the existing invariant applied to a different
unit of account.

**What happens when a trade nets to nothing:** a self-match where both legs
land on the *same* cash account and the *same* position account (same
owner, same accounts on both sides of the trade) merges to zero on every
account it touches — `validate_postings()`'s existing account-merge step
already collapses two opposite postings on the same account, and if *both*
pairs collapse, fewer than two accounts remain, which is exactly the
existing "transaction affects fewer than two accounts" rejection
(`UnbalancedTransaction`, unchanged since M2). `app/services/trading.py`
catches that specific exception around a trade's settlement and treats it as
"this trade had zero net economic effect — nothing to post," while still
recording the `Trade` row (a fill genuinely happened, from the matcher's
point of view) and updating both orders' filled quantity. This is safe to
catch narrowly here — and only here — because these four postings are
constructed by the caller from a single price and quantity, so the *only*
way they can ever fail balance validation is exactly this self-cancelling
case; any other `UnbalancedTransaction` at this call site would mean a
different, real bug in how the postings were built, not an expected
condition. `Trade.ledger_transaction_id` is nullable for exactly this case.

**The gap this accepts, honestly:** because funds aren't reserved at order
entry (Decision 3), a resting order's settlement can still fail with a
normal `InsufficientFunds` if its owner's balance changed after it started
resting. This project's `place_order()` treats that as a real error: the
entire incoming order's transaction rolls back (no order row, no trade, no
book change) and the caller sees a 422 — the same shape `POST /payments`
already uses for `InsufficientFunds`. The resting order itself is
untouched, since nothing committed. A production venue would never let this
happen in the first place (Decision 3); this project accepts it as a rare
edge case out of scope to fully close.

## Alternatives rejected

- **Matching inside the database** (a stored procedure, or matching logic
  expressed as SQL over the `orders` table directly). Rejected for the same
  reason Decision 1 argues for a pure function: SQL is a poor language for
  expressing "walk this sorted list, consuming from the front, across two
  sides, with a running remainder" and would be effectively untestable
  without a live database for every case.
- **An `instrument_id` + `quantity` column on `accounts`/`ledger_entries`
  instead of reusing `currency`.** More explicit about what a position
  account IS, but doubles the columns `ledger.post()` has to reason about
  and gives up the exact property the roadmap asks for — "a trade is a
  ledger transaction like any other" stops being true the moment position
  accounts need different code paths than cash accounts.
- **Self-trade prevention inside the matcher.** Rejected in Decision 3 — it
  couples price-time matching to an identity concern the roadmap's scope
  doesn't ask for, and the ledger already has a well-defined (if narrow)
  answer for what a same-owner match settles to.

## Consequences

- The matcher's purity is a promise the rest of the system has to keep: if
  `app/services/trading.py` ever needs the matcher to know about an
  account balance, a fee schedule, or anything else that requires the
  database, that's a sign the matcher's scope is being asked to grow beyond
  price-time priority, and the right response is a new function, not an I/O
  call smuggled into `match()`.
- A wash (self-match) trade is recorded in `trades` with
  `ledger_transaction_id = NULL`. Any future reporting or reconciliation
  over trades has to treat that as "this trade moved zero real money,"
  not as missing data.
- Widening `currency` to `VARCHAR(16)` and relaxing its CHECK constraint
  means the database can no longer distinguish a real ISO currency code from
  an instrument symbol by shape alone — a typo'd instrument symbol that
  happens to collide with a real currency code (unlikely, not impossible)
  would be silently accepted. Not guarded against here; flagged as a known
  narrow edge.
- Every future consumer of `ledger_entries` (the reconciliation job, M4's
  read-model style projections, an eventual statement/report feature) now
  has to group by `currency` before summing anything, or it will silently
  add rupees to share counts. This wasn't a concern before M6, because
  there was only ever one currency in play per transaction.

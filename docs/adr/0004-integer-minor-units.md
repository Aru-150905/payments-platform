# ADR 0004 — Money as BIGINT minor units

**Status:** accepted · **Date:** 2026-09

## Decision

All monetary amounts are `BIGINT` in the currency's smallest unit (paise for
INR, cents for USD), named `*_minor`. The API accepts and returns minor units.

## Why not float

`0.1 + 0.2 == 0.30000000000000004`. Binary floating point cannot represent most
decimal fractions, so errors accumulate across millions of entries and the
ledger stops summing to zero.

## Why not NUMERIC/Decimal

`NUMERIC` is exact and a defensible choice. Integers were chosen because they
are exact *and* cheap to sum, index and compare, and because there is no
rounding-mode decision to get wrong at every boundary. The cost is that
division (fees, splits) must decide explicitly where the remainder goes — which
is a feature: that decision should be visible, not implicit in a rounding mode.

## Consequence

`int64` caps at ~9.2×10^18 minor units. Ample for any realistic amount, and the
`CHECK (amount_minor <> 0)` and `> 0` constraints catch the common mistakes.

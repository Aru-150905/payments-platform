# ADR 0003 — Modular monolith, not microservices

**Status:** accepted · **Date:** 2026-09

## Context

The system has clear domains: ledger, payments, and later orders and matching.
The obvious-looking move is a service per domain.

## Decision

One deployable application with hard internal module boundaries
(`app/db`, `app/domain`, `app/services`, `app/api`, `app/events`), plus two
separate *processes* from the same codebase: the outbox relay and the Kafka
consumer.

## Why

The ledger's core guarantee is that a payment's status change and its ledger
entries commit atomically. Splitting payments and ledger into separate services
with separate databases turns that single transaction into a saga with
compensating actions — strictly harder, strictly more failure modes, and the
compensations themselves can fail.

Splitting the *processes* while keeping one database gets the actual benefit
people want from microservices here: the API, the relay and the consumer scale
and fail independently, without giving up transactional integrity.

## When to revisit

When one module's write throughput or team ownership genuinely diverges. Module
boundaries are already drawn so that extraction is a refactor, not a rewrite.

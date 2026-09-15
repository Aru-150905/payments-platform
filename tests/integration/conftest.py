"""
Fixtures for the integration suite only. Scoped to this directory on purpose
— an autouse fixture here must never reach into `tests/test_*.py`, which are
the fast, infra-free unit tests the CI job depends on staying that way.

Every test that uses these fixtures assumes the compose stack's Postgres is
up, migrated, and seeded: `make up && make migrate && make seed`. See ADR
0005 for why isolation between test runs is "everyone uses unique data," not
"wipe the database" — in short: these tests exist to prove repeated
invocations behave correctly, so the suite depends on the same repeatability
it's testing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.db.base import engine
from app.events.consumer import _new_consumer
from app.events.consumer import drain_once as _project_once
from app.events.producer import start_producer, stop_producer
from app.events.relay import drain_once as _relay_once
from app.main import app

pytestmark = pytest.mark.integration


def unique(prefix: str) -> str:
    """A token no other test run has used. The whole isolation strategy rests on this."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="session", autouse=True)
def _require_postgres() -> None:
    """
    Fail with one clear instruction instead of a wall of connection-refused
    tracebacks, one per test, if the stack isn't running.

    Pings with a bare asyncpg connection, never through app.db.base's
    SQLAlchemy engine: that engine is a module-level singleton whose pool
    binds connections to the event loop they were opened on. This fixture
    runs its own throwaway loop via asyncio.run() (session-scoped fixtures
    run before pytest-asyncio's per-test loop exists), and touching the
    shared pool from that loop poisons it — every real test afterwards then
    fails with "Future attached to a different loop" the moment it tries to
    reuse a pooled connection.
    """
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def ping() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()

    try:
        asyncio.run(ping())
    except Exception as exc:  # noqa: BLE001 - a setup gate, not app logic
        pytest.exit(
            "Integration tests need Postgres up, migrated, and seeded: "
            "`make up && make migrate && make seed`. "
            f"Connection attempt failed: {exc!r}",
            returncode=1,
        )


@pytest.fixture(autouse=True)
async def _fresh_pool_per_test() -> AsyncIterator[None]:
    """
    app.db.base.engine is a module-level singleton — it has to be, it's the
    app's real production-shaped engine, reused by every request the ASGI
    transport makes. Its asyncpg pool binds each connection to the event loop
    it was opened on, but pytest-asyncio hands every test function a fresh
    loop (`asyncio_default_fixture_loop_scope = "function"`). Without this,
    test 2 inherits test 1's pooled connections, tries to use them on a loop
    that's already closed, and fails with "Future attached to a different
    loop" — a failure about test plumbing, not about payments. Disposing
    after every test forces the pool to open fresh connections next time,
    under whatever loop is current then.
    """
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _fresh_producer_per_test() -> AsyncIterator[None]:
    """
    app.events.producer holds its AIOKafkaProducer in a module-level global,
    same shape as app.db.base's engine and the same hazard: it binds to the
    event loop it started on, and pytest-asyncio hands every test a fresh
    one. start_producer() is a no-op if a producer already exists, so
    without this, test 2 would silently reuse test 1's producer — bound to
    test 1's already-closed loop — instead of starting its own. Stopping it
    resets the module global to None, so the next test that calls
    start_producer() gets a fresh one under whatever loop is current then.
    """
    yield
    await stop_producer()


async def drain_pipeline() -> int:
    """
    Drains the outbox relay AND the read-model consumer to quiescence: every
    unpublished outbox row gets published, then every unprojected Kafka
    message gets applied to read_model_payments. Returns how many events the
    consumer side projected.

    Used instead of running the relay/worker as background processes during
    tests, so a test controls exactly when its events move rather than
    racing a long-running loop with an unpredictable poll interval — the
    same reason relay.py and consumer.py each expose a single-shot
    drain_once() rather than only the infinite run().
    """
    await start_producer()
    while await _relay_once():
        pass

    consumer = _new_consumer()
    await consumer.start()
    try:
        total = 0
        while True:
            n = await _project_once(consumer)
            total += n
            if n == 0:
                break
        return total
    finally:
        await consumer.stop()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """
    Talks to the app in-process over real ASGI/HTTP semantics — status codes,
    headers, the works — without a separately running uvicorn. That matters
    here: both bugs this suite targets were bugs in how routes.py mapped an
    exception to a status code, which a direct service-function call would
    never exercise.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://integration-test") as ac:
        yield ac


async def create_account(
    client: AsyncClient, *, allow_negative: bool, currency: str = "INR"
) -> dict:
    resp = await client.post(
        "/accounts",
        json={
            "owner_id": unique("owner"),
            "name": "wallet",
            "currency": currency,
            "allow_negative": allow_negative,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def payer_payee(client: AsyncClient) -> tuple[dict, dict]:
    """
    payer allow_negative=True (a pre-authorized facility, same shape as
    scripts/demo.sh's wallet) so tests skip a funding step entirely; payee is
    an ordinary wallet. Fresh accounts per test, per ADR 0005 — never shared
    seed data, so nothing here can collide with a previous run.
    """
    payer = await create_account(client, allow_negative=True)
    payee = await create_account(client, allow_negative=False)
    return payer, payee


async def balance_of(client: AsyncClient, account_id: str) -> int:
    resp = await client.get(f"/accounts/{account_id}/balance")
    assert resp.status_code == 200, resp.text
    return resp.json()["balance_minor"]


async def authorize(
    client: AsyncClient,
    payer: dict,
    payee: dict,
    *,
    amount_minor: int = 5_000,
    key: str | None = None,
) -> dict:
    resp = await client.post(
        "/payments",
        headers={"Idempotency-Key": key or unique("idem")},
        json={
            "payer_account_id": payer["id"],
            "payee_account_id": payee["id"],
            "amount_minor": amount_minor,
            "currency": "INR",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()

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

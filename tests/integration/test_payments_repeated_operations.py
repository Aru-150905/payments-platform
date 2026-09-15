"""
Integration tests against the real API + Postgres.

The 17 unit tests in tests/ check validate_postings() and
assert_can_transition() as pure functions — a single call, no database, no
way to ever hit a unique constraint. They cannot catch a bug that only shows
up on the SECOND call against the same entity. Two such bugs shipped the
night this file was written:

  - capture()/void() wrote to the ledger before checking the state machine,
    so a repeated capture hit uq_ledger_tx_idempotency and surfaced as a raw
    500 instead of the 409 an illegal transition should be (fixed 5954ac8).
  - scripts/demo.sh reused a fixed idempotency key across runs, so every run
    after the first silently returned the first run's payment (fixed cb602af).

Every test below is built around calling something twice against the same
payment/account/key, not around the first-time happy path (that's what the
unit tests and demo.sh already cover). See ADR 0005 for how the suite itself
stays repeatable without a manual database reset between runs.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.integration.conftest import authorize, balance_of, create_account, unique

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# repeated payment creation (idempotency key reuse)
# --------------------------------------------------------------------------

async def test_repeated_creation_with_same_key_returns_the_same_payment(client, payer_payee):
    payer, payee = payer_payee
    key = unique("idem")

    first = await authorize(client, payer, payee, amount_minor=5_000, key=key)
    second = await authorize(client, payer, payee, amount_minor=5_000, key=key)

    assert second["id"] == first["id"]


async def test_repeated_creation_ignores_a_changed_body(client, payer_payee):
    """
    The idempotency key, not the request body, is what makes a retry safe —
    demo.sh's bug (reusing a key across genuinely different requests) is the
    same failure shape from the other side. If the second call's amount won,
    a client that retried with an edited payload could silently authorize
    the wrong amount against the first call's payment.
    """
    payer, payee = payer_payee
    key = unique("idem")

    first = await authorize(client, payer, payee, amount_minor=5_000, key=key)
    second = await authorize(client, payer, payee, amount_minor=9_999, key=key)

    assert second["id"] == first["id"]
    assert second["amount_minor"] == 5_000


async def test_repeated_creation_moves_money_exactly_once(client, payer_payee):
    payer, payee = payer_payee
    key = unique("idem")
    before_payee = await balance_of(client, payee["id"])

    await authorize(client, payer, payee, amount_minor=5_000, key=key)
    await authorize(client, payer, payee, amount_minor=5_000, key=key)

    # authorize moves payer -> clearing, not payer -> payee, so the payee's
    # balance is untouched either way; what must NOT double is the payer's.
    assert await balance_of(client, payer["id"]) == -5_000
    assert await balance_of(client, payee["id"]) == before_payee


async def test_concurrent_creation_with_same_key_agrees_on_one_payment(client, payer_payee):
    """
    Two requests racing on a network retry, not two sequential calls. This is
    the IntegrityError-then-reread path in create_and_authorize(): both
    inserts are attempted, one wins the unique constraint, the other must
    read back the winner rather than erroring or creating a second payment.
    """
    payer, payee = payer_payee
    key = unique("idem")
    transport = ASGITransport(app=app)

    async def call() -> dict:
        async with AsyncClient(transport=transport, base_url="http://integration-test") as ac:
            resp = await ac.post(
                "/payments",
                headers={"Idempotency-Key": key},
                json={
                    "payer_account_id": payer["id"],
                    "payee_account_id": payee["id"],
                    "amount_minor": 5_000,
                    "currency": "INR",
                },
            )
            assert resp.status_code == 201, resp.text
            return resp.json()

    first, second = await asyncio.gather(call(), call())

    assert first["id"] == second["id"]
    assert await balance_of(client, payer["id"]) == -5_000


# --------------------------------------------------------------------------
# repeated capture / void (the state-machine-before-ledger bug)
# --------------------------------------------------------------------------

async def test_repeated_capture_is_409_not_500(client, payer_payee):
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=5_000)

    first = await client.post(f"/payments/{payment['id']}/capture")
    assert first.status_code == 200, first.text

    second = await client.post(f"/payments/{payment['id']}/capture")
    assert second.status_code == 409, second.text


async def test_repeated_capture_moves_money_exactly_once(client, payer_payee):
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=5_000)
    before = await balance_of(client, payee["id"])

    await client.post(f"/payments/{payment['id']}/capture")
    await client.post(f"/payments/{payment['id']}/capture")  # rejected, must be a no-op

    after = await balance_of(client, payee["id"])
    assert after - before == 5_000


async def test_repeated_void_is_409_not_500(client, payer_payee):
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=5_000)

    first = await client.post(f"/payments/{payment['id']}/void")
    assert first.status_code == 200, first.text

    second = await client.post(f"/payments/{payment['id']}/void")
    assert second.status_code == 409, second.text


async def test_repeated_void_moves_money_exactly_once(client, payer_payee):
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=5_000)
    before = await balance_of(client, payer["id"])  # authorize already moved payer by -5000

    await client.post(f"/payments/{payment['id']}/void")
    await client.post(f"/payments/{payment['id']}/void")  # rejected, must be a no-op

    after = await balance_of(client, payer["id"])
    assert after - before == 5_000  # exactly one reversal, not two


async def test_capture_after_void_is_409_not_500(client, payer_payee):
    """A terminal state rejecting a transition is the same code path as a
    repeat — both are IllegalTransition, both must be 409."""
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=5_000)

    voided = await client.post(f"/payments/{payment['id']}/void")
    assert voided.status_code == 200, voided.text

    captured = await client.post(f"/payments/{payment['id']}/capture")
    assert captured.status_code == 409, captured.text


async def test_void_after_capture_is_409_not_500(client, payer_payee):
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=5_000)

    captured = await client.post(f"/payments/{payment['id']}/capture")
    assert captured.status_code == 200, captured.text

    voided = await client.post(f"/payments/{payment['id']}/void")
    assert voided.status_code == 409, voided.text


async def test_concurrent_repeated_capture_exactly_one_wins(client, payer_payee):
    """
    Fires two captures at the same payment at once, rather than one after the
    other. This is what test_repeated_capture_is_409_not_500 cannot exercise:
    with a purely sequential retry, the FIRST capture is always long
    committed before the SECOND is even sent, so `_locked()`'s SELECT FOR
    UPDATE never actually has to block anyone. A real double-click or a
    client's automatic retry racing the original request does contend on
    that lock, and it's the combination of the row lock (this test) and the
    unique constraint backstop (test_repeated_capture_is_409_not_500) that
    the fix in 5954ac8 relies on.
    """
    payer, payee = payer_payee
    payment = await authorize(client, payer, payee, amount_minor=5_000)
    before = await balance_of(client, payee["id"])
    transport = ASGITransport(app=app)

    async def capture() -> int:
        async with AsyncClient(transport=transport, base_url="http://integration-test") as ac:
            resp = await ac.post(f"/payments/{payment['id']}/capture")
            return resp.status_code

    results = await asyncio.gather(capture(), capture())

    assert sorted(results) == [200, 409]
    after = await balance_of(client, payee["id"])
    assert after - before == 5_000


# --------------------------------------------------------------------------
# whole-suite repeatability
# --------------------------------------------------------------------------

async def test_full_lifecycle_is_independently_repeatable(client):
    """
    Runs the entire authorize -> capture, authorize -> void lifecycle twice
    end-to-end inside one test, with fresh accounts and keys each time —
    the same shape as running `pytest -m integration` itself twice in a row.
    If any piece of this suite's own isolation were broken (a shared account,
    a reused key), the second pass would fail where the first passed.
    """
    for _ in range(2):
        payer = await create_account(client, allow_negative=True)
        payee = await create_account(client, allow_negative=False)

        payment = await authorize(client, payer, payee, amount_minor=1_234)
        capture = await client.post(f"/payments/{payment['id']}/capture")
        assert capture.status_code == 200, capture.text
        assert await balance_of(client, payee["id"]) == 1_234

        second_payment = await authorize(client, payer, payee, amount_minor=1_000)
        void = await client.post(f"/payments/{second_payment['id']}/void")
        assert void.status_code == 200, void.text
        # payer moved -1234 (captured, stays moved) and -1000 then +1000 (voided, reverses)
        assert await balance_of(client, payer["id"]) == -1_234

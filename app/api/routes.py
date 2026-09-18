from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_api_key
from app.db.base import get_session
from app.db.models import Account, AccountType, Balance, Payment
from app.domain.state_machine import IllegalTransition
from app.services import ledger, payments

# Applied to every route below, not per-endpoint — see ADR 0007 Decision 4.
# /health/* and /metrics are defined directly on `app` in main.py, outside
# this router, and deliberately stay unauthenticated (a load balancer and
# Prometheus can't be handed an API key to poll them with).
router = APIRouter(dependencies=[Depends(require_api_key)])


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

class AccountIn(BaseModel):
    owner_id: str
    name: str
    account_type: AccountType = AccountType.LIABILITY
    currency: str = "INR"
    allow_negative: bool = False


class AccountOut(BaseModel):
    id: uuid.UUID
    owner_id: str
    name: str
    currency: str
    balance_minor: int


class PaymentIn(BaseModel):
    payer_account_id: uuid.UUID
    payee_account_id: uuid.UUID
    # Minor units, so the API can never be handed 19.99 and round it.
    amount_minor: int = Field(gt=0)
    currency: str = "INR"


class PaymentOut(BaseModel):
    id: uuid.UUID
    status: str
    amount_minor: int
    currency: str

    @classmethod
    def of(cls, p: Payment) -> PaymentOut:
        return cls(
            id=p.id, status=p.status.value,
            amount_minor=p.amount_minor, currency=p.currency,
        )


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

@router.post("/accounts", response_model=AccountOut, status_code=201)
async def create_account(body: AccountIn, session: AsyncSession = Depends(get_session)):
    async with session.begin():
        account = Account(**body.model_dump())
        session.add(account)
        await session.flush()
        # Every account gets its balance row up front, so ledger.post() can
        # assume it exists and lock it. Creating it lazily would mean two
        # concurrent first-postings racing to insert the same row.
        session.add(
            Balance(
                account_id=account.id,
                currency=account.currency,
                balance_minor=0,
            )
        )

    return AccountOut(
        id=account.id, owner_id=account.owner_id, name=account.name,
        currency=account.currency, balance_minor=0,
    )


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(
    owner_id: str | None = None, session: AsyncSession = Depends(get_session)
):
    """
    Read-only, for the frontend's account picker/table — nothing else in
    this codebase needed "every account," only "one account by id," which
    is why this didn't exist before. A single join against Balance instead
    of N+1 calls to ledger.get_balance() per row.
    """
    query = select(Account, Balance).join(Balance, Balance.account_id == Account.id)
    if owner_id is not None:
        query = query.where(Account.owner_id == owner_id)
    rows = (await session.execute(query.order_by(Account.created_at.desc()).limit(500))).all()
    return [
        AccountOut(
            id=a.id, owner_id=a.owner_id, name=a.name,
            currency=a.currency, balance_minor=b.balance_minor,
        )
        for a, b in rows
    ]


@router.get("/accounts/{account_id}/balance")
async def get_balance(account_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    try:
        balance = await ledger.get_balance(session, account_id)
        return {"account_id": account_id, "balance_minor": balance}
    except ledger.LedgerError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# --------------------------------------------------------------------------
# payments
# --------------------------------------------------------------------------

@router.post("/payments", response_model=PaymentOut, status_code=201)
async def create_payment(
    body: PaymentIn,
    session: AsyncSession = Depends(get_session),
    # Required, not optional. Making idempotency opt-in means every client that
    # forgets the header can double-charge someone on a flaky connection.
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    try:
        async with session.begin():
            payment = await payments.create_and_authorize(
                session,
                idempotency_key=idempotency_key,
                payer_account_id=body.payer_account_id,
                payee_account_id=body.payee_account_id,
                amount_minor=body.amount_minor,
                currency=body.currency,
            )
    except IntegrityError:
        # Lost a race against a concurrent request carrying the same
        # Idempotency-Key — both passed create_and_authorize()'s initial
        # "does this key exist yet?" check before either had committed, so
        # both tried to insert. `async with session.begin()` above has
        # already rolled the failed attempt back cleanly; read back the
        # request that won and return ITS payment, so the loser's client
        # sees the same idempotent result the winner's does.
        existing = (
            await session.execute(
                select(Payment).where(Payment.idempotency_key == idempotency_key)
            )
        ).scalar_one()
        return PaymentOut.of(existing)
    except ledger.InsufficientFunds as exc:
        # 422, not 500. The request was well-formed; the world said no.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except (ledger.LedgerError, payments.PaymentError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return PaymentOut.of(payment)


@router.post("/payments/{payment_id}/capture", response_model=PaymentOut)
async def capture_payment(payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    return await _mutate(session, payments.capture, payment_id)


@router.post("/payments/{payment_id}/void", response_model=PaymentOut)
async def void_payment(payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    return await _mutate(session, payments.void, payment_id)


@router.get("/payments", response_model=list[PaymentOut])
async def list_payments(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(Payment).order_by(Payment.created_at.desc()).limit(500))
    ).scalars().all()
    return [PaymentOut.of(p) for p in rows]


@router.get("/payments/{payment_id}", response_model=PaymentOut)
async def get_payment(payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payment not found")
    return PaymentOut.of(payment)


async def _mutate(session: AsyncSession, fn, payment_id: uuid.UUID) -> PaymentOut:
    try:
        async with session.begin():
            payment = await fn(session, payment_id)
    except IllegalTransition as exc:
        # 409 Conflict is the right code: the request is valid, but the
        # resource is not in a state where it can be honoured.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrityError as exc:
        # Backstop, not the primary defence: assert_can_transition in
        # payments.py should catch a repeated capture/void before this ever
        # fires. Kept in case a future caller reaches ledger.post() without
        # going through that check first — same failure (a stale unique
        # constraint on the ledger transaction), same 409, no 500.
        raise HTTPException(status.HTTP_409_CONFLICT, "operation already applied") from exc
    except ledger.InsufficientFunds as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except (ledger.LedgerError, payments.PaymentError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return PaymentOut.of(payment)
